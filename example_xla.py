# Copyright (c) Meta Platforms, Inc. and affiliates.
# This software may be used and distributed according to the terms of the GNU General Public License version 3.

from typing import Tuple
import os
import sys
import torch
import fire
import time
import json
from pathlib import Path

from llama import ModelArgs, Transformer, Tokenizer, LLaMA
from llama.xla_model_parallel import get_model_parallel_rank, get_model_parallel_world_size

import os

USE_CUDA = bool(int(os.environ.get('USE_CUDA', 0)))
USE_XLA = bool(int(os.environ.get('USE_XLA', 0)))
USE_TORCH_DYNAMO = bool(int(os.environ.get('USE_TORCH_DYNAMO', 1)))
GPU_NUM_DEVICES = int(os.environ.get("GPU_NUM_DEVICES", 0))


# Some how xla init will slow down the CUDA speed.
if USE_XLA:
    import torch_xla.core.xla_model as xm
    import torch_xla.distributed.xla_multiprocessing as xmp


def setup_model_parallel() -> Tuple[int, int]:
    # assuming model parallelism over the whole world size
    rank = get_model_parallel_rank()
    world_size = get_model_parallel_world_size()

    # seed must be the same in all processes
    torch.manual_seed(1)
    device = xm.xla_device()
    xm.set_rng_state(1, device=device)
    return rank, world_size


def load(
    ckpt_dir: str,
    tokenizer_path: str,
    rank: int,
    world_size: int,
    max_seq_len: int,
    max_batch_size: int,
    device: torch.device,
    dim: int = 4096,
    n_layers: int = 32,
    n_heads: int = 32,
    quant: bool = False,
) -> LLaMA:
    start_time = time.time()
    print("Loading")
    if ckpt_dir:
        # load checkpoint if ckpt_dir is provided
        checkpoints = sorted(Path(ckpt_dir).glob("*.pth"))
        assert world_size == len(
            checkpoints
        ), f"Loading a checkpoint for MP={len(checkpoints)} but world size is {world_size}"
        ckpt_path = checkpoints[rank]
        checkpoint = torch.load(ckpt_path, map_location="cpu")
        with open(Path(ckpt_dir) / "params.json", "r") as f:
            params = json.loads(f.read())
    else:
        params = {
            "dim": dim,
            "n_layers": n_layers,
            "n_heads": n_heads,
            "quant": quant,
        }

    model_args: ModelArgs = ModelArgs(max_seq_len=max_seq_len,
                                      max_batch_size=max_batch_size,
                                      **params)
    tokenizer = Tokenizer(model_path=tokenizer_path)
    model_args.vocab_size = tokenizer.n_words
    if USE_CUDA:
        torch.set_default_tensor_type(torch.cuda.HalfTensor)
    else:
        torch.set_default_tensor_type(torch.BFloat16Tensor)
    model = Transformer(model_args)
    if ckpt_dir:
        model.load_state_dict(checkpoint, strict=False)
    model = model.to(device)
    for i in range(len(model.cache_kvs)):
        model.cache_kvs[i] = tuple(t.to(device) for t in model.cache_kvs[i])
    torch.set_default_tensor_type(torch.FloatTensor)

    generator = LLaMA(model, tokenizer)
    print(f"Loaded in {time.time() - start_time:.2f} seconds")
    return generator


def main(
    tokenizer_path: str,
    temperature: float = 0.8,
    top_p: float = 0.95,
    max_seq_len: int = 512,
    max_batch_size: int = 32,
    ckpt_dir: str = '',
    dim: int = 4096,
    n_layers: int = 32,
    n_heads: int = 32,
    quant: bool = False,
    n_times: int = 3,
):
    rank, world_size = setup_model_parallel()
    if rank > 0:
        sys.stdout = open(os.devnull, "w")

    generator = load(ckpt_dir, tokenizer_path,
                     rank, world_size, max_seq_len, max_batch_size,
                     xm.xla_device(), dim, n_layers, n_heads, quant)

    prompts = [
        # For these prompts, the expected answer is the natural continuation of the prompt
        "I believe the meaning of life is",
        # "Simply put, the theory of relativity states that ",
        # "Building a website can be done in 10 simple steps:\n",
        # Few shot prompts: https://huggingface.co/blog/few-shot-learning-gpt-neo-and-inference-api
        #        """Tweet: "I hate it when my phone battery dies."
        # Sentiment: Negative
        # ###
        # Tweet: "My day has been 👍"
        # Sentiment: Positive
        # ###
        # Tweet: "This is the link to the article"
        # Sentiment: Neutral
        # ###
        # Tweet: "This new music video was incredibile"
        # Sentiment:""",
        #        """Translate English to French:
        
        # sea otter => loutre de mer
        
        # peppermint => menthe poivrée
        
        # plush girafe => girafe peluche
        
        # cheese =>""",
    ]
    # warmup
    with torch.no_grad():
            results = generator.generate(prompts,
                                         256,
                                         xm.xla_device(),
                                         temperature=temperature,
                                         top_p=top_p)
    for _ in range(n_times):
        with torch.no_grad():
            results = generator.generate(prompts,
                                         256,
                                         xm.xla_device(),
                                         temperature=temperature,
                                         top_p=top_p)

        for result in results:
            print(result)
            print("\n==================================\n")
    print("XLA:GPU ENV INFO:")
    print(f"USE_XLA: {bool(USE_XLA)}, USE_CUDA: {bool(USE_CUDA)}, USE_TORCH_DYNAMO: {bool(USE_TORCH_DYNAMO)}, NUM_GPU(S): {GPU_NUM_DEVICES}")
    print(f"Run LLaMA model in {ckpt_dir} for {n_times} prompts with {max_batch_size} max batch size(s)")
    print(f"\t- mean latency: {sum(generator.latency_list[1:]) / n_times}(s)")
    print(f"\t- mean per-token latency: {sum(generator.per_token_latency_list[1:]) / n_times * 1000}(ms/token)")


def _fn(
    idx,
    tokenizer_path: str,
    temperature: float = 0.8,
    top_p: float = 0.95,
    max_seq_len: int = 512,
    max_batch_size: int = 32,
    ckpt_dir: str = '',
    dim: int = 4096,
    n_layers: int = 32,
    n_heads: int = 32,
    quant: bool = False,
    n_times: int = 3,
):
    main(tokenizer_path, temperature, top_p, max_seq_len, max_batch_size,
         ckpt_dir, dim, n_layers, n_heads, quant, n_times)


def mp_main(
    mp: bool,
    tokenizer_path: str,
    temperature: float = 0.8,
    top_p: float = 0.95,
    max_seq_len: int = 512,
    max_batch_size: int = 32,
    ckpt_dir: str = '',
    dim: int = 4096,
    n_layers: int = 32,
    n_heads: int = 32,
    quant: bool = False,
    n_times: int = 3,
):
    if mp and USE_XLA:
        xmp.spawn(_fn,
                  args=(tokenizer_path, temperature, top_p, max_seq_len,
                        max_batch_size, ckpt_dir, dim, n_layers, n_heads,
                        quant, n_times))
    else:
        main(tokenizer_path, temperature, top_p, max_seq_len, max_batch_size,
             ckpt_dir, dim, n_layers, n_heads, quant, n_times)


if __name__ == "__main__":
    fire.Fire(mp_main)
