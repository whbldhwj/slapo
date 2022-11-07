import torch
import torch.nn as nn
import torch.fx as fx
import torch.distributed as dist
import ms


def replace_layernorm(sch):
    print("Replace LayerNorm with FusedLayerNorm")
    from apex.normalization.fused_layer_norm import FusedLayerNorm

    ops = sch.find_module(lambda node: "LayerNorm" in node.target)
    for op in ops:
        sch[op].replace(FusedLayerNorm)


def replace_gelu(sch):
    # https://github.com/NVIDIA/Megatron-LM/blob/master/megatron/model/fused_bias_gelu.py
    print("Replace GeLU with FusedBiasGeLU")
    from ms.op import BiasGeLU

    raise RuntimeError("Not correct! Should fuse with previous linear bias!")
    ops = sch.find_function(lambda node: "gelu" in str(node.target))
    for op in ops:
        sch[op].replace(BiasGeLU)


def replace_xformer_attention(sch, config):
    # https://github.com/huggingface/transformers/blob/344e2664d450eaa9167ce31f7d1fc0f0fe3b10af/src/transformers/models/bert/modeling_bert.py#L243
    # https://github.com/comaniac/epoi/blob/main/epoi/ops/xformers_attn.py#L45
    print("Replace HF BertSelfAttention with xformer Attention")
    from ms.op import BertSelfAttentionXFormer

    ops = sch.find_module(lambda node: ".attention.self" in node.target)
    for op in ops:
        sch[op].replace(BertSelfAttentionXFormer, config=config)


def replace_qkv(sch, hidden_size, num_heads, num_layers):
    print("Replace HF QKV Dense with FusedQKV")

    class FusedQKV(nn.Module):
        def __init__(self, hidden_size, num_heads) -> None:
            super(FusedQKV, self).__init__()
            self.hidden_size = hidden_size
            self.num_heads = num_heads
            self.head_size = hidden_size // num_heads
            self.fused_linear = nn.Linear(hidden_size, num_heads * self.head_size * 3)

        def transpose_for_scores(self, x):
            new_x_shape = x.size()[:-1] + (self.num_heads // sch.world_size, self.head_size, 3)
            x = x.view(new_x_shape)
            return x.permute(0, 2, 1, 3, 4)

        def forward(self, hidden_states):  # [8, 512, 768]
            qkv = self.fused_linear(hidden_states)
            transposed_qkv = self.transpose_for_scores(qkv)
            q, k, v = torch.split(transposed_qkv, 1, dim=-1)
            q = torch.squeeze(q)
            k = torch.squeeze(k)
            v = torch.squeeze(v)
            return [q, k, v]

    class QKV_Pattern(ms.Pattern):
        def __init__(self, layer_num):
            super(QKV_Pattern, self).__init__()
            self.layer_num = layer_num

        @staticmethod
        def func(x: torch.Tensor) -> torch.Tensor:
            new_x_shape = x.size()[:-1] + (num_heads, hidden_size)
            x = x.view(new_x_shape)
            return x.permute(0, 2, 1, 3)

        def starting_point(self, node):
            if node.op != "call_module":
                return False
            name = node.target
            if "layer.{}.".format(self.layer_num) in name and "self" in name:
                if "query" in name or "key" in name or "value" in name:
                    return True
            return False

    for i in range(num_layers):
        op_lst = sch.find(QKV_Pattern(i))
        sch[op_lst].replace(FusedQKV, hidden_size=hidden_size, num_heads=num_heads)


def shard_params(sch, config, fused_qkv=False, prefix=""):
    prefix = "" if prefix == "" else prefix + "."

    # Embedding
    sch[prefix+"embeddings.word_embeddings"].shard("weight", axis=0)
    # Build the mask
    vocab_start_index = sch.rank * config.vocab_size // sch.world_size
    vocab_end_index = (sch.rank + 1) * config.vocab_size // sch.world_size
    def fw_pre_hook(_input):
        # Mask the input
        input_mask = (_input[0] < vocab_start_index) | (_input[0] >= vocab_end_index)
        masked_input = _input[0].clone() - vocab_start_index
        masked_input[input_mask] = 0
        return masked_input
    sch[prefix+"embeddings.word_embeddings"].hook("fw_pre", fw_pre_hook)
    def fw_post_hook(_input, output):
        # Mask the output embedding
        input_mask = (_input[0] < vocab_start_index) | (_input[0] >= vocab_end_index)
        output[input_mask, :] = 0.0
        # Reduce across all the model parallel GPUs
        dist.all_reduce(output, op=dist.ReduceOp.SUM)
        return output
    sch[prefix+"embeddings.word_embeddings"].hook("fw_post", fw_post_hook)

    for i in range(config.num_hidden_layers):
        # MLP
        sch[prefix+"encoder.layer.{}.intermediate.dense".format(i)].shard("weight", axis=0)
        sch[prefix+"encoder.layer.{}.intermediate.dense".format(i)].shard("bias", axis=0)
        sch[prefix+"encoder.layer.{}.output.dense".format(i)].shard("weight", axis=1)
        sch[prefix+"encoder.layer.{}.output.dense".format(i)].sync()
        sch[prefix+"encoder.layer.{}.intermediate.dense".format(i)].sync(backward=True)

        # Attention
        if not fused_qkv:
            sch[prefix+"encoder.layer.{}.attention.self.query".format(i)].shard("weight", axis=0)
            sch[prefix+"encoder.layer.{}.attention.self.key".format(i)].shard("weight", axis=0)
            sch[prefix+"encoder.layer.{}.attention.self.value".format(i)].shard("weight", axis=0)
            sch[prefix+"encoder.layer.{}.attention.self.query".format(i)].shard("bias", axis=0)
            sch[prefix+"encoder.layer.{}.attention.self.key".format(i)].shard("bias", axis=0)
            sch[prefix+"encoder.layer.{}.attention.self.value".format(i)].shard("bias", axis=0)
            sch[prefix+"encoder.layer.{}.attention.self.query".format(i)].sync(backward=True)
            sch[prefix+"encoder.layer.{}.attention.self.key".format(i)].sync(backward=True)
            sch[prefix+"encoder.layer.{}.attention.self.value".format(i)].sync(backward=True)
        else:
            sch_fusedqkv = ms.create_schedule(
                sch.get_module(prefix+"encoder.layer.{}.attention.self.FusedQKV_0".format(i)),
                world_size=sch.world_size,
                rank=sch.rank,
                tracer="pytorch"
            )
            sch_fusedqkv["fused_linear"].shard("weight", axis=0)
            sch_fusedqkv["fused_linear"].shard("bias", axis=0)
            sch_fusedqkv["fused_linear"].sync(backward=True)
            opt_fusedqkv, _ = ms.build(sch_fusedqkv)
            sch[prefix+"encoder.layer.{}.attention.self.FusedQKV_0".format(i)].replace(opt_fusedqkv)
            # sch[prefix+"encoder.layer.{}.attention.self.FusedQKV_0".format(i)].shard("weight", axis=0)
            # sch[prefix+"encoder.layer.{}.attention.self.FusedQKV_0".format(i)].shard("bias", axis=0)
            # sch[prefix+"encoder.layer.{}.attention.self.FusedQKV_0".format(i)].sync(backward=True)

        sch[prefix+"encoder.layer.{}.attention.output.dense".format(i)].shard("weight", axis=1)
        sch[prefix+"encoder.layer.{}.attention.output.dense".format(i)].sync()

    # fix number of heads
    import operator
    ops = sch.find_method(lambda node:
        node.target == "view" and # args[0] is self
        isinstance(node.args[1], fx.Node) and
        node.args[1].op == "call_function" and
        node.args[1].target == operator.add)

    def new_view(tensor, old_shape):
        new_shape = old_shape[:-1] + (-1,)
        return tensor.view(new_shape)

    for op in ops:
        sch[op].replace(new_view)
