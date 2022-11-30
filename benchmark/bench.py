import argparse
import math
import os
import re
from dataclasses import dataclass

import matplotlib.pyplot as plt
import torch
from transformers import AutoConfig


@dataclass
class Exp:
    name: str  # Experiment name
    model: str  # huggingface model name
    batch_size: int  # batch size per GPU
    seq_len: int  # input sequence length
    seq_len_dec: int  # Decoder sequence length. Encoder-decoder model only.

    ## Improve speed / reduce memory
    bf16: bool = False  # Faster, less memory. Recommend if GPU supports
    fp16: bool = False  # Faster, less memory, but need to scale loos.
    optim: str = "adamw_hf"  # Optimization method
    grad_ckpt: str = ""  # Empty means no checkpointing; otherwise:
    # Megatron: "full" or "selective".
    # HF w. schedule: a floating point indicating
    # the checkpointing ratio. For example, 0.5 means
    # to checkpoint a half of layers.
    grad_accum: int = 1  # accumulate gradients for better performance
    steps: int = 40  # number of parameter updates

    ## Multi-GPUs
    gpus: str = "0"  # GPUs to use. "0,1" means use GPU 0 and 1
    tensor_para: int = 1  # Tensor parallelism

    ## kwargs
    kwargs: dict = None

    def __post_init__(self):
        model_conf = AutoConfig.from_pretrained(self.model)
        get = lambda *keys: max(
            [getattr(model_conf, k) if hasattr(model_conf, k) else 0 for k in keys]
        )
        self.num_layers = get("num_hidden_layers", "n_layer")
        self.num_gpus = len(self.gpus.split(","))
        self.hidden_size = get("hidden_size", "n_embd", "d_model")
        self.vocab_size = get("vocab_size")
        self.num_heads = get("num_attention_heads", "n_head")
        if self.seq_len_dec == 0:
            # Encoder or decoder only models.
            n, h, s, v = (
                self.num_layers,
                self.hidden_size,
                self.seq_len,
                self.vocab_size,
            )
            att, ffn, embed = (
                4 * h * s**2 + 8 * s * h**2,
                16 * s * h**2,
                2 * s * h * v,
            )
            forward = n * (att + ffn) + embed
            # TFLOPs to train one example. Note that we use model TFLOPS instead of
            # hardware TFLOPS, so having checkpoints or not does not matter.
            self.tflops = 3 * forward / 1e12
        else:
            # Encoder-decoder models.
            self.num_decoder_layers = get('num_decoder_layers')
            self.d_kv = get("d_kv")
            self.d_ff = get("d_ff")

            h, s_e, s_d, v = (
                self.hidden_size,
                self.seq_len,
                self.seq_len_dec,
                self.vocab_size,
            )

            # If not specified in HF config, num_decoder_layers are the same as num_layers.
            l_e, l_d = self.num_layers, self.num_decoder_layers

            # Calculate TFLOPS of T5.
            gated = False  # HF/Megatron T5 don't gate by default.

            # Note that we use model TFLOPS instead of
            # hardware TFLOPS, so having checkpoints or not does not matter.
            c = 3  # 4 if self.grad_ckpt else 3

            enc_flops = 1 + s_e / 6 / h
            if gated:
                enc_flops += 1 / 3 + 1 / 6 / h
            enc_flops *= c * l_e * 24 * s_e * h**2

            dec_flops = (
                1
                + 1 / 6
                + s_e / 6 / s_d
                + s_d / 6 / h
                + s_e / 6 / h
                + v / 4 / c / l_d / h
            )
            if gated:
                dec_flops += 1 / 3 + 1 / 6 / h
            dec_flops *= 24 * c * s_d * l_d * h**2

            # TFLOPs to train one example
            self.tflops = (enc_flops + dec_flops) / 1e12
        self.launcher = f"torchrun --nproc_per_node {self.num_gpus}"

    def print_results(self):
        print("Total samples / second\t: %.1f" % self.samples_per_sec)
        print("Per GPU memory (GB)\t: %.1f" % self.gpu_mem)
        print(
            "Per GPU TFLOPs\t\t: %.1f"
            % (self.samples_per_sec * self.tflops / self.num_gpus)
        )


def parse_args():
    common_parser = argparse.ArgumentParser(add_help=False)
    common_parser.add_argument("--model", type=str, help="Model name")
    common_parser.add_argument(
        "--error-stop",
        action="store_true",
        help="Stop when error occurs. Note that out-of-memory is not considdered as error",
    )
    common_parser.add_argument(
        "--seq-len", type=int, default=512, help="Sequence length. Default: 512"
    )
    common_parser.add_argument(
        "--seq-len-dec",
        type=int,
        default=0,
        help="Decoder sequence length. Only used by encoder-decoder models. Default: 0",
    )
    common_parser.add_argument(
        "--dtype", type=str, default="fp16", help="Model dtype. Default: fp16"
    )
    common_parser.add_argument(
        "--gpus",
        type=str,
        default="pow2",
        help="Number of GPUs to be used. Options: "
        "1. A single number (e.g., 1); "
        "2. A comma separated list of GPU numbers (e.g., 1,2); "
        "3. A string 'pow2' (e.g., pow2) to cover power of 2 GPUs (e.g., 1,2,4,8).",
    )
    common_parser.add_argument(
        "--batch-sizes",
        type=str,
        default="8 * int(math.log2(n) + 1)",
        help="An expression with `n` as GPU number to to calculate batch size. `math` can be used."
        "Default: 8 * int(math.log2(n) + 1)",
    )
    common_parser.add_argument(
        "--gradient-checkpoint",
        type=str,
        default="",
        help="Gradient checkpointing. Empty means no checkpointing; otherwise: "
        "Megatron: 'full' or 'selective'. HF w. schedule: a floating point indicating "
        "the checkpointing ratio. For example, 0.5 means to checkpoint a half of layers.",
    )

    parser = argparse.ArgumentParser()
    subprasers = parser.add_subparsers(
        dest="impl", help="Model implementation (hf or megatron)"
    )
    hf_parser = subprasers.add_parser(
        "hf",
        parents=[common_parser],
        help="HuggingFace model",
    )
    hf_parser.add_argument(
        "script_file",
        type=str,
        help="Megatron script file to run the HuggingFace model",
    )
    hf_parser.add_argument(
        "--disable-flash-attn",
        action="store_true",
        help="Do not replace Attention with FlashAttention in HuggingFace model",
    )
    mt_parser = subprasers.add_parser(
        "megatron",
        parents=[common_parser],
        help="Megatron model",
    )
    mt_parser.add_argument(
        "--disable-fuse-kernels",
        action="store_true",
        help="Disable fusion kernels in Megatron models.",
    )
    return parser.parse_args()


def parse_gpus(gpus):
    n_gpu = torch.cuda.device_count()
    if gpus == "pow2":
        n_gpus = [2**i for i in range(int(math.log2(n_gpu)) + 1)]
    elif "," in gpus:
        n_gpus = [int(e) for e in gpus.split(",")]
    else:
        n_gpus = [int(gpus)]

    assert (
        min(n_gpus) > 0 and max(n_gpus) <= n_gpu
    ), f"GPU numbers must be in 0 - {n_gpu}, but got {n_gpus}"

    print("GPUs to be used\t:")
    for i in range(max(n_gpus)):
        print(f"GPU{i}\t\t:", torch.cuda.get_device_name(i))

    return n_gpus


def compare(exps, fig_name):
    _, ax = plt.subplots(ncols=3, figsize=(9, len(exps) / 2))
    x = list(range(len(exps)))
    for i, (y, l) in enumerate(
        (
            ([e.samples_per_sec for e in exps], "Samples / sec"),
            (
                [e.samples_per_sec * e.tflops / e.num_gpus for e in exps],
                "per GPU TFLOPS",
            ),
            ([e.gpu_mem for e in exps], "per GPU memory (GB)"),
        )
    ):
        bar = ax[i].barh(
            x, y, align="center", height=0.6, color=plt.get_cmap("Set1")(x)
        )
        ax[i].bar_label(bar, fmt="%.2f", label_type="center")
        ax[i].invert_yaxis()
        ax[i].set_xlabel(l)
        if i == 0:
            ax[i].set_yticks(x, labels=[e.name for e in exps])
        else:
            ax[i].set_yticklabels([])

    plt.title(fig_name)
    file_name = fig_name.replace(" ", "-").replace("/", "-").replace("|", "-")
    plt.savefig(f"{file_name}.png", format="png", dpi=200, bbox_inches="tight")
    print(f"Result saved to {file_name}.png")
    plt.show()


def megatron_bert_cmd(exp, script_file=None):
    if script_file is None:
        import megatron

        path = megatron.__path__[0]
        script_file = f"{path}/../pretrain_bert.py"

    return (
        script_file,
        [
            f"--seq-length {exp.seq_len}",
            f"--max-position-embeddings {exp.seq_len}",
            "--data-path bert-sample_text_sentence",
            "--vocab-file bert-large-uncased-vocab.txt",
        ],
    )


def megatron_gpt_cmd(exp, script_file=None):
    if script_file is None:
        import megatron

        path = megatron.__path__[0]
        script_file = f"{path}/../pretrain_gpt.py"

    return (
        script_file,
        [
            f"--seq-length {exp.seq_len}",
            f"--max-position-embeddings {exp.seq_len}",
            "--data-path gpt2-sample_text_document",
            "--vocab-file gpt2-vocab.json",
            "--merge-file gpt2-merges.txt",
        ],
    )


def megatron_t5_cmd(exp, script_file=None):
    if script_file is None:
        import megatron

        path = megatron.__path__[0]
        script_file = f"{path}/../pretrain_t5.py"

    assert hasattr(exp, "d_kv") and hasattr(exp, "d_ff")
    return (
        script_file,
        [
            f"--encoder-seq-length {exp.seq_len}",
            f"--decoder-seq-length {exp.seq_len_dec}",
            f"--max-position-embeddings {exp.seq_len}",
            f"--kv-channels {exp.d_kv}",
            f"--ffn-hidden-size {exp.d_ff}",
            "--data-path bert-sample_text_sentence",
            "--vocab-file bert-large-uncased-vocab.txt",
            "--vocab-extra-ids 100",
        ],
    )


MEGATRON_COMMAND_BY_MODEL = {
    "bert": megatron_bert_cmd,
    "gpt": megatron_gpt_cmd,
    "t5": megatron_t5_cmd,
}


def megatron_log(exp, log_filename):
    with open(log_filename) as f:
        text = f.read()
    # Find the last number after the key, returns 0 if not exists
    def query(key, last_only=True):
        values = re.findall(key + ": +([\d\.]+)", text)
        if not values:
            return None
        if last_only:
            return float(values[-1])
        return [float(v) for v in values]

    if "CUDA out of memory" in text:
        print("Out of GPU memory, try a smaller batch size")
        exp.error_code = 1
        return exp

    iter_times = query("elapsed time per iteration \(ms\)", last_only=False)
    if not iter_times:
        print(f'Failed. Check "{log_filename}" to find error')
        exp.error_code = 2
        return exp

    # 1. Every 5 steps, Megatron reports the average iteration time of the past 5 steps.
    # 2. We remove the first value (of the first 5 steps) as the warmup.
    avg_time = lambda times: (sum(times[1:]) * 5) / (exp.steps - 5)

    iter_time = avg_time(iter_times)
    forward_compute_time = avg_time(query("forward-compute", last_only=False))
    backward_compute_time = avg_time(query("backward-compute", last_only=False))
    backward_param_all_reduce_time = avg_time(
        query("backward-params-all-reduce", last_only=False)
    )
    optimizer_time = avg_time(query("optimizer", last_only=False))

    param_per_gpu = query(
        "parameters on \(tensor, pipeline\) model parallel rank \(0, 0\)"
    )
    exp.samples_per_sec = query("global batch size") / iter_time * 1e3
    exp.gpu_mem = query("max allocated") / 1e3
    print(f"per GPU params\t\t: {param_per_gpu / 1e6:.2f}M")
    print(
        f"Breakdown(ms)\t\t: total {iter_time:.2f}, forward {forward_compute_time:.2f}, "
        f"backward {backward_compute_time:.2f}, "
        f"backward-params-all-reduce {backward_param_all_reduce_time:.2f}, "
        f"optimizer {optimizer_time:.2f}"
    )
    exp.error_code = 0
    return exp


def run_megatron(exp, args):
    script_file = args.script_file if args.impl == "hf" else None
    for model_key, gen in MEGATRON_COMMAND_BY_MODEL.items():
        if model_key in exp.model:
            script_file, data_args = gen(exp, script_file)
            break
    else:
        raise ValueError(f"Unsupported model {exp.model}")

    cmd = f"""MODEL_NAME={exp.model} {exp.launcher} {script_file} \
--num-layers {exp.num_layers} --hidden-size {exp.hidden_size} \
--num-attention-heads {exp.num_heads} \
--tensor-model-parallel-size {exp.tensor_para} \
--micro-batch-size {exp.batch_size} \
--train-iters {exp.steps} {' '.join(data_args)} \
--data-impl mmap --lr 0.00015 --log-interval 5 --eval-iters 1"""
    if exp.grad_ckpt:
        if args.impl == "hf":
            # Gradient checkpoint ratio for HF schedule is passed via environment variable.
            grad_ckpt = "full"
        else:
            if exp.grad_ckpt == "full":
                cmd += f" --recompute-method uniform"
            grad_ckpt = exp.grad_ckpt
        cmd += f" --recompute-granularity {grad_ckpt}"
    if exp.bf16:
        cmd += " --bf16"
    if exp.fp16:
        cmd += " --fp16"

    if exp.kwargs is not None:
        if "flags" in exp.kwargs:
            cmd += " " + " ".join(exp.kwargs["flags"])
        if "env" in exp.kwargs:
            cmd = f"{' '.join(exp.kwargs['env'])} {cmd}"

    cmd += " > log.txt 2>&1"
    print(cmd)
    os.system(cmd)
    ret = megatron_log(exp, "log.txt")
    if ret.error_code != 0:
        ret.samples_per_sec = 0
        ret.gpu_mem = 0
    else:
        ret.print_results()
    return ret


def main():
    args = parse_args()
    assert args.dtype == "fp16", "Only fp16 is supported for now"

    print("Pytorch version\t:", torch.__version__)
    print("CUDA version\t:", torch.version.cuda)

    title = f"{'Megatron' if args.impl == 'megatron' else 'HF'} {args.model}"
    memo = ""

    n_gpus = parse_gpus(args.gpus)
    get_batch_size = eval(f"lambda n: {args.batch_sizes}", {"math": math})

    # Deal with configurations.
    kwargs = {}
    if hasattr(args, "disable_fuse_kernels") and args.disable_fuse_kernels:
        kwargs = {
            "flags": [
                "--no-bias-gelu-fusion",
                "--no-bias-dropout-fusion",
                "--no-persist-layer-norm",
                "--no-masked-softmax-fusion",
            ]
        }
        memo += "|no_fuse"
    if hasattr(args, "disable_flash_attn") and args.disable_flash_attn:
        kwargs = {"env": ["DISABLE_FLASH_ATTN=1"]}
        memo += "|no_flash_attn"
    if args.gradient_checkpoint:
        memo += f"|grad_ckpt {args.gradient_checkpoint}"

    results = []
    for n_gpu in n_gpus:
        gpus = ",".join([str(e) for e in range(n_gpu)])
        batch_size = get_batch_size(n_gpu)
        results.append(
            run_megatron(
                Exp(
                    f"BS{batch_size} ({n_gpu} GPU)",
                    args.model,
                    batch_size,
                    args.seq_len,
                    args.seq_len_dec,
                    grad_ckpt=args.gradient_checkpoint,
                    fp16=args.dtype == "fp16",
                    gpus=gpus,
                    tensor_para=n_gpu,
                    kwargs=kwargs,
                ),
                args,
            )
        )
        if results[-1].error_code == 2 and args.error_stop:
            print("Stop benchmarking due to error")
            break
    compare(results, f"{title}{memo}")


if __name__ == "__main__":
    main()
