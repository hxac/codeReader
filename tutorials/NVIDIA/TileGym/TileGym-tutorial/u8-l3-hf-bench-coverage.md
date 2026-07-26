# HF 推理基准与内核覆盖率

## 1. 本讲目标

上一讲（u8-l1）我们讲了 monkey-patch 如何把 TileGym 内核「塞进」HuggingFace 模型。但塞进去之后，怎么回答两个工程问题：

1. **到底快了多少？** —— 需要一个能跑端到端推理、并打印吞吐量的基准工具。
2. **到底有多少 GPU 计算「真的」落在了 TileGym 内核上？** —— monkey-patch 可能漏掉某个层、可能某条路径没走融合，需要一个客观的、基于 GPU kernel 执行时间统计的「覆盖率」指标。

本讲围绕 `modeling/transformers` 这个 uv 子项目展开，读完你应当掌握：

- `tilegym-hf-bench` CLI 的关键参数（`--use_tilegym` / `--use_cutile` / `--use_attn` / `--profile` / `--report_kernel_coverage`）与预设脚本。
- 两条 profiling 路径：`torch.profiler`（生成 Chrome trace / CSV）与 `nsys`（系统级 GPU kernel 抓取）。
- kernel coverage 报告的两个比值（GPU 时间占比、launch 次数占比）如何被算出来。
- `KernelFilter` 的「子串匹配 + 黑名单」机制如何决定一个 kernel 算不算「TileGym/cuTile 内核」。

## 2. 前置知识

本讲默认你已经学完：

- **u2-l2（dispatcher.py）**：理解 `@register_impl` 把后端实现挂到全局注册表 `_REGISTRY` 的「算子名」键下。
- **u8-l1（monkey-patch）**：理解 `apply_tilegym_kernel_to_*` 在模型实例化**之前**替换 RoPE/RMSNorm/MLP/Attention，`use_cutile` 既切后端又可能追加融合补丁。

此外需要几个本讲才出现的术语，先做通俗解释：

- **uv 子项目**：`modeling/transformers` 有自己独立的 `pyproject.toml` 和 `uv.lock`，像一个小型独立包，通过 editable 模式把父仓库的 `tilegym` 当依赖。它不是 `src/tilegym` 的一部分，而是一个「消费」TileGym 的下游基准工具。
- **nsys（Nsight Systems）**：NVIDIA 的系统级性能分析器，能抓取整张 GPU 上真正执行过的每一个 CUDA kernel 的起止时间，结果存成 `.nsys-rep`（本质是一个 SQLite 库）。它观察的是「硬件上发生了什么」，比应用层日志更权威。
- **CUPTI**：CUDA Profiling Tools Interface，是 nsys/torch profiler 底层用来读取 kernel 事件的 API。本讲会直接在 SQL 里看到 `CUPTI_ACTIVITY_KIND_KERNEL` 这张表。
- **demangled name（反修饰名）**：C++ 编译器会把 `silu_and_mul(...)` 修饰成一串怪符号（mangled name），nsys 存的是还原回人可读的 `demangledName`。本讲的 kernel 名字匹配用的就是这种可读名。
- **capture range**：nsys 的 `-c cudaProfilerApi` 模式不会全程录像，而是等程序主动调用 `cudaProfilerStart()` 才开始、调用 `cudaProfilerStop()` 才结束。本讲会看到 TileGym 如何利用这个机制只抓「一次前向」。

## 3. 本讲源码地图

本讲全部源码都在 `modeling/transformers/` 这个 uv 子项目内，分布如下：

| 文件 | 作用 |
| --- | --- |
| [modeling/transformers/infer.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/modeling/transformers/infer.py) | 旧版入口的「兼容垫片（shim）」，把 `src/` 加进 `sys.path` 后转调新版 `main`。 |
| [modeling/transformers/src/tilegym_hf_bench/_cli.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/modeling/transformers/src/tilegym_hf_bench/_cli.py) | CLI 主逻辑：参数解析、后端判定、warmup/计时循环、分发到 profiling/coverage。 |
| [modeling/transformers/src/tilegym_hf_bench/tilegym_patch.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/modeling/transformers/src/tilegym_hf_bench/tilegym_patch.py) | 按 `model_id` 字符串匹配，转调 u8-l1 讲过的 `apply_tilegym_kernel_to_*`。 |
| [modeling/transformers/src/tilegym_hf_bench/forward.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/modeling/transformers/src/tilegym_hf_bench/forward.py) | `NaiveForwardWrapper`：封装 `model.generate(...)` 为一个可反复调用的 `forward()`。 |
| [modeling/transformers/src/tilegym_hf_bench/hf_shim.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/modeling/transformers/src/tilegym_hf_bench/hf_shim.py) | 模型/分词器加载，带本地缓存优先策略。 |
| [modeling/transformers/src/tilegym_hf_bench/profiling/torch_profiler.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/modeling/transformers/src/tilegym_hf_bench/profiling/torch_profiler.py) | `--profile` 路径：torch profiler 抓 trace、过滤后打印、导出 CSV/zip。 |
| [modeling/transformers/src/tilegym_hf_bench/profiling/nsys.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/modeling/transformers/src/tilegym_hf_bench/profiling/nsys.py) | `--report_kernel_coverage` 路径：fork 一个子进程跑在 nsys 下，再读 SQLite 算覆盖率。 |
| [modeling/transformers/src/tilegym_hf_bench/kernel_filters/filters.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/modeling/transformers/src/tilegym_hf_bench/kernel_filters/filters.py) | `KernelFilter`：根据 YAML 配置判断一个 kernel 名「算不算 TileGym 内核」。 |
| [modeling/transformers/src/tilegym_hf_bench/kernel_filters/tilegym_kernel_prefixes.yaml](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/modeling/transformers/src/tilegym_hf_bench/kernel_filters/tilegym_kernel_prefixes.yaml) | 匹配规则配置：`prefixes` 白名单子串 + `blacklist` 黑名单。 |
| [modeling/transformers/scripts/benchmark_hf_model.sh](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/modeling/transformers/scripts/benchmark_hf_model.sh) | 一键脚本：按模型预设跑 baseline / cutile / coverage 三轮。 |
| [modeling/transformers/pyproject.toml](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/modeling/transformers/pyproject.toml) | 声明 `tilegym-hf-bench` 控制台脚本入口与依赖。 |

记住一句话总览：**`_cli.py` 是大脑，`kernel_filters` 是判官，`profiling/{torch_profiler,nsys}.py` 是两条腿。**

## 4. 核心概念与源码讲解

### 4.1 CLI 参数与预设：怎么发起一次基准

#### 4.1.1 概念说明

这个子项目对外提供三个等价入口，最终都汇入同一个 `main()`：

1. **控制台脚本** `tilegym-hf-bench`（推荐）：由 `pyproject.toml` 的 `[project.scripts]` 声明，`uv run tilegym-hf-bench ...` 调用。
2. **模块调用** `python -m tilegym_hf_bench._cli`：nsys 子进程用的就是这一种。
3. **遗留脚本** `python infer.py`：infer.py 是个「垫片（shim）」，仅把 `src/` 插入 `sys.path` 后转调新版 `main`，保留它是为了不破坏旧文档/脚本。

三个入口共享同一份参数与主流程，差别只在「怎么找到 `tilegym_hf_bench` 这个包」。

#### 4.1.2 核心流程

一次普通基准的执行流：

```
parse_args(argv)
  └─ 读 --use_tilegym / --use_cutile / --use_attn / --model_id / --output_length ...
main()
  ├─ 若 --report_kernel_coverage：转交 NsysKernelCoverageReporter，直接 return（见 4.4）
  ├─ load tokenizer / model
  ├─ _detect_backend(args)  → "base" 或 "cutile"
  ├─ 若 use_tilegym：apply_tilegym_patch(...)   ← u8-l1 的 monkey-patch，必须早于模型实例化
  ├─ warmup_runs 次预热
  ├─ num_runs 次正式计时（cuda.Event 计时 + tokens/sec 统计）
  ├─ 打印 BENCHMARK RESULTS
  └─ 若 --profile：run_torch_profiler(...)      ← 见 4.3
```

注意 `--use_tilegym` / `--use_cutile` / `--use_attn` 都是布尔开关（`action="store_true"`），它们的组合语义需要特别记：

- `--use_tilegym`：总开关，决定是否打 TileGym 补丁。
- `--use_cutile`：仅在 `--use_tilegym` 之下有意义，把后端切到 cuTile（否则走默认/triton 兜底）。看 `_detect_backend`：只有「`use_tilegym` 且 `use_cutile`」才返回 `"cutile"`，否则 `"base"`。
- `--use_attn`：是否连注意力一起替换（RoPE/RMSNorm/MLP 默认开，注意力按这个开关才开）。

#### 4.1.3 源码精读

**垫片入口** `_SRC` 路径插入 + 转调，这就是为什么 `python infer.py` 仍能工作：

[infer.py:10-17](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/modeling/transformers/infer.py#L10-L17)：把 `src/` 目录塞进 `sys.path[0]`，使 `import tilegym_hf_bench` 可见，然后 `from tilegym_hf_bench._cli import main`，在 `__main__` 里调用 `main()`。

**控制台脚本入口**声明在 pyproject 里，指向同一个 `main`：

[pyproject.toml:50-51](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/modeling/transformers/pyproject.toml#L50-L51)：`tilegym-hf-bench = "tilegym_hf_bench._cli:main"`。

**三个关键开关**的参数定义（全在 `parse_args`）：

[_cli.py:22](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/modeling/transformers/src/tilegym_hf_bench/_cli.py#L22)（`--use_tilegym`）、[_cli.py:35-37](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/modeling/transformers/src/tilegym_hf_bench/_cli.py#L35-L37)（`--use_attn` / `--use_cutile` / `--profile`）、[_cli.py:48-52](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/modeling/transformers/src/tilegym_hf_bench/_cli.py#L48-L52)（`--report_kernel_coverage`，docstring 已点明它会「在 nsys 下跑并报告 GPU 时间和 launch 次数占比」）。

**后端判定** `_detect_backend`，解释了「为什么单独给 `--use_cutile` 不生效」：

[_cli.py:74-79](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/modeling/transformers/src/tilegym_hf_bench/_cli.py#L74-L79)：默认 `"base"`，仅当 `use_tilegym and use_cutile` 才 `"cutile"`。

**patch 时机**（承接 u8-l1，强调「必须早于 `load_model_with_cache`」）：

[_cli.py:126-139](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/modeling/transformers/src/tilegym_hf_bench/_cli.py#L126-L139)：先 `_detect_backend`，再 `apply_tilegym_patch(model_id, use_attn, use_cutile=...)`，**之后**才 `load_model_with_cache`。顺序一旦颠倒，「替换类」类补丁就来不及生效。

**warmup + 计时循环**：

[_cli.py:159-196](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/modeling/transformers/src/tilegym_hf_bench/_cli.py#L159-L196)：先用 `warmup_runs` 次预热；正式轮用一对 `torch.cuda.Event(enable_timing=True)`（`start_event`/`end_event`）包住 `forward_wrapper.forward()`，再 `torch.cuda.synchronize()` 后取 `elapsed_time`，统计 `tokens/sec`。这正是 u8-l1 提到的「monkey-patch 后用真实推理测速」。

**预设脚本**把常见模型一键化，免去手写一长串参数：

[benchmark_hf_model.sh](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/modeling/transformers/scripts/benchmark_hf_model.sh)（`llama` 预设段）：每个 `--model-key` 对应一组默认的 `MODEL_ID / INPUT_FILE / OUTPUT_LENGTH / BATCH_SIZE`，脚本依次跑 baseline（`--profile`）、cuTile（`--use_tilegym --use_cutile --use_attn --profile`）、coverage（`--report_kernel_coverage`）三轮，并写 summary 文件。

#### 4.1.4 代码实践

**实践目标**：在不实际下载模型权重的前提下，验证 CLI 参数解析与后端判定逻辑。

**操作步骤**：

1. 阅读 [README.md:30-43](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/modeling/transformers/README.md#L30-L43) 的「Basic Inference」两条命令，对比「naive」与「tilegym+cutile+attn」的参数差异。
2. 在 `_cli.py` 的 `_detect_backend` 处脑内推演四种开关组合的返回值。
3. （可选，待本地验证）用 `--help` 触发 `parse_args` 而不加载模型：
   ```bash
   cd modeling/transformers
   uv run tilegym-hf-bench --help
   ```

**需要观察的现象**：`--help` 能正常打印所有参数说明，证明 CLI 可用且无需 GPU/模型。

**预期结果**：四种组合的 `_detect_backend` 输出如下表（待本地验证）：

| `--use_tilegym` | `--use_cutile` | backend |
| --- | --- | --- |
| 否 | — | `base` |
| 是 | 否 | `base` |
| 是 | 是 | `cutile` |

#### 4.1.5 小练习与答案

**练习 1**：为什么 `python infer.py` 能在没装 `tilegym-hf-bench` 包的情况下也跑通？

**答案**：因为 [infer.py:10-14](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/modeling/transformers/infer.py#L10-L14) 把 `src/` 目录手动插入 `sys.path`，使 `tilegym_hf_bench` 直接以源码形式被 import，不依赖 `pip install` 装出来的包。

**练习 2**：若把 `_cli.py` 中的 `apply_tilegym_patch(...)` 移到 `load_model_with_cache(...)` 之后，会发生什么？

**答案**：u8-l1 讲过，「替换类」补丁通过替换模块属性生效，必须在模型用这些类实例化之前完成。移到加载之后，模型层对象已经用原生 HuggingFace 类构造好了，patch 来不及生效，等于没打补丁。

---

### 4.2 kernel 名称过滤：KernelFilter 的「子串 + 黑名单」

#### 4.2.1 概念说明

无论 torch profiler 还是 nsys，都会给出一长串 kernel 名字（成百上千个）。其中只有一部分是 TileGym/cuTile 发出的，其余是 PyTorch/cuDNN/cuBLAS 等原生 kernel。要算「覆盖率」，必须先回答：**给一个 kernel 名，怎么判定它算不算 TileGym 的？**

`KernelFilter` 就是这个判官。它的规则来自一份 YAML：一份「白名单子串」列表 + 一份「黑名单」列表。注意一个关键细节：尽管 YAML 字段叫 `prefixes`、README 也说「prefix matching」，**源码用的是 Python 的 `in` 运算符（子串包含），不是 `startswith`（真前缀）**。这是最容易踩的坑，下一节细讲。

#### 4.2.2 核心流程

```
KernelFilter.__init__
  └─ _load_list_yaml("tilegym_kernel_prefixes.yaml")
       ├─ self.kernel_names_prefix = config["prefixes"]   # 白名单子串列表
       └─ self.blacklist_kernel_names = config["blacklist"]  # 黑名单（精确相等）
contains(key):
  for prefix in prefixes:
      if (prefix in key) and (key not in blacklist):   # 子串包含 + 整串不在黑名单
          return True
  return False
```

判定规则（逐字对应源码）：

- 遍历每一个白名单子串 `prefix`；
- 若 `prefix` 作为**子串**出现在 kernel 名 `key` 里，**且** `key` 的**完整字符串**不在黑名单里，就算命中；
- 任一子串命中即返回 `True`；全部不命中才 `False`。

#### 4.2.3 源码精读

**KernelFilter 全貌**（构造 + `contains`）：

[filters.py:15-28](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/modeling/transformers/src/tilegym_hf_bench/kernel_filters/filters.py#L15-L28)：`__init__` 读 YAML 拿到两个列表；`contains` 用 `prefix in key`（子串）配 `key not in self.blacklist_kernel_names`（整串黑名单）做判定。注意是 `prefix in key`，不是 `key.startswith(prefix)`。

**白名单子串**（节选，可见覆盖了 RoPE / Attention / MoE / norm / matmul / 门控等各类 TileGym 内核）：

[tilegym_kernel_prefixes.yaml:7-45](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/modeling/transformers/src/tilegym_hf_bench/kernel_filters/tilegym_kernel_prefixes.yaml#L7-L45)：例如 `rope`、`prefill_fmha`、`fmha_kernel`、`attention_decode_kernel`、`fused_moe_kernel`、`silu_and_mul`、`rms_norm_kernel`、`static_persistent_matmul_kernel`、`group_gemm`、`matmul`、宽泛兜底的 `tile_` 与 `cutile` 等。

**黑名单**正是为修补「子串误伤」而存在：

[tilegym_kernel_prefixes.yaml:47-48](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/modeling/transformers/src/tilegym_hf_bench/kernel_filters/tilegym_kernel_prefixes.yaml#L47-L48)：`blacklist: ["aten::matmul"]`。因为白名单里有子串 `matmul`，而 PyTorch 原生算子名恰好是 `aten::matmul`——若没有这条黑名单，原生 matmul 会被误算成 TileGym 内核，覆盖率虚高。这条黑名单是「子串匹配 vs 前缀匹配」差异的活证据。

> **示例代码**（非项目代码，仅用于演示 `in` 与 `startswith` 的差异）：
> ```python
> key = "aten::matmul"
> "matmul" in key                 # True  ← 子串匹配，会误命中
> key.startswith("matmul")        # False ← 真·前缀匹配，不会误命中
> key in ["aten::matmul"]         # True  ← 黑名单按整串相等拦截
> ```

#### 4.2.4 代码实践

**实践目标**：亲手验证「子串匹配 + 黑名单」会怎样影响一个 kernel 的归类。

**操作步骤**：

1. 在仓库里打开 `tilegym_kernel_prefixes.yaml`，找到 `matmul` 与 `aten::matmul` 这一对。
2. 阅读 `filters.py` 的 `contains`，确认它用的是 `in`。
3. **源码阅读型推演**：假设 nsys 抓到三个 kernel 名——`static_persistent_matmul_kernel`、`aten::matmul`、`cutile_silu_and_mul_kernel`，按下表判断 `contains` 各自返回什么。
4. （可选，待本地验证）若把 `contains` 改成 `key.startswith(prefix)`，问 `cutile_silu_and_mul_kernel` 是否还能被 `silu_and_mul` 命中？（答：不能，因为它以 `cutile_` 开头；这也解释了为什么项目作者选择更宽松的 `in`。）

**需要观察的现象**：子串匹配能命中「名字里带有该片段」的任意 kernel，无论片段出现在首、中、尾。

**预期结果**（待本地验证）：

| kernel 名 | `contains` 结果 | 命中的子串 / 拦截原因 |
| --- | --- | --- |
| `static_persistent_matmul_kernel` | `True` | 含 `static_persistent_matmul_kernel` |
| `aten::matmul` | `False` | 含 `matmul`，但整串在黑名单 |
| `cutile_silu_and_mul_kernel` | `True` | 含 `silu_and_mul`（也含 `cutile`） |

#### 4.2.5 小练习与答案

**练习 1**：为什么 YAML 里 `prefixes` 最后还要放 `tile_` 和 `cutile` 这种「宽泛兜底」子串？

**答案**：cuTile 内核编译后的 demangled 名通常带 `cutile` 或 `tile_` 前缀（如 `tile_rope_kernel`）。放宽泛子串能兜住那些没被逐条枚举的新内核，避免「新加了一个 cuTile 内核但忘了登记到 YAML」导致覆盖率漏算。

**练习 2**：若某天 PyTorch 把原生 RMSNorm kernel 命名为 `aten::rms_norm_kernel`，现有配置会出现什么问题？怎么修？

**答案**：白名单含 `rms_norm_kernel`，子串匹配会把 `aten::rms_norm_kernel` 误判为 TileGym 内核，覆盖率虚高。修法是在 `blacklist` 里追加 `aten::rms_norm_kernel`（仿照 `aten::matmul` 的处理）。

---

### 4.3 profiling 路径之一：torch profiler（`--profile`）

#### 4.3.1 概念说明

`--profile` 走的是 **应用内** profiling：用 `torch.profiler.profile` 在 Python 层抓 CPU + CUDA 活动，产出 Chrome trace（`.json`，可塞进 `chrome://tracing` 或 Perfetto 看）和 CSV 明细。它轻量、无需 root、能在任何环境跑，但抓到的是「PyTorch 视角」的 kernel 事件（带 op 名、shape 聚合），适合定位「哪个 Python op 慢」。

这条路径还有个隐藏的「双重身份」：函数里有一段 `cudaProfilerStart()/cudaProfilerStop()` 包住一次前向，专门给 nsys 的 `cudaProfilerApi` 捕获模式当「开关」用——4.4 节会看到，这恰恰是 kernel coverage 能只抓一次前向的关键。

#### 4.3.2 核心流程

```
main() 末尾：if args.profile: run_torch_profiler(forward_wrapper, args, case_id, avg_time, summary_line)
run_torch_profiler:
  ├─ with profile(CPU, CUDA):
  │     record_function("model_inference"): forward()        ← torch profiler 抓取
  ├─ cudaProfilerStart(); forward(); cudaProfilerStop()      ← 给 nsys 的 capture range 开关
  ├─ KernelFilter 过滤 key_averages()，打印 FILTERED RESULTS
  ├─ 导出 profiler_results_<case_id>_<ts>.csv（含每项 Filtered 标记）
  ├─ 导出 chrome trace → 打包成 trace_<case_id>_<ts>.zip
  └─ 若 --summary_file：把 CUDA kernel 总时间(ms) 追加到 summary 行
```

#### 4.3.3 源码精读

**profile + Start/Stop 双段**：

[torch_profiler.py:17-31](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/modeling/transformers/src/tilegym_hf_bench/profiling/torch_profiler.py#L17-L31)：第一段 `with profile(...)` 包一次 `record_function("model_inference")` 前向，用于 `key_averages()` 统计；第二段 `torch.cuda.cudart().cudaProfilerStart()` / `Stop()` 包另一次前向——这次本身不产生 torch profiler 数据，而是为 nsys 的 `-c cudaProfilerApi` 提供「开始/结束」信号。当此程序**不是**跑在 nsys 下时，这两行相当于空操作；**当跑在 nsys 下**（即 coverage 模式的子进程），它们划定 nsys 的捕获区间。

**用 KernelFilter 过滤并打印**：

[torch_profiler.py:33-73](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/modeling/transformers/src/tilegym_hf_bench/profiling/torch_profiler.py#L33-L73)：遍历 `prof.key_averages()`，仅保留 `kernel_filter.contains(item.key)` 为真的项，打印 CPU/CUDA 总时间与均值、调用次数，并累加 `total_device_time` 与单次推理耗时 `avg_time` 的占比。

**导出 CSV（带 Filtered 列）与 zip 化 trace**：

[torch_profiler.py:82-103](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/modeling/transformers/src/tilegym_hf_bench/profiling/torch_profiler.py#L82-L103)：CSV 每行末尾写 `kernel_filter.contains(item.key)`，即该 kernel 是否算 TileGym；chrome trace 先导出再压成 zip 以减小体积。

#### 4.3.4 代码实践

**实践目标**：用 `--profile` 产出一份 trace，找到「被 Filter 判定为 TileGym」的 kernel 列表。

**操作步骤**：

1. 准备好可访问的模型缓存（或在有 GPU 的机器上）。
2. 运行（待本地验证）：
   ```bash
   cd modeling/transformers
   uv run tilegym-hf-bench \
       --model_id meta-llama/Meta-Llama-3.1-8B \
       --use_tilegym --use_cutile --use_attn \
       --profile --num_runs 5 \
       --log_dir /logs
   ```
3. 打开 `/logs/profiler_results_<case_id>_<ts>.csv`，筛 `Filtered` 列为 `True` 的行。
4. 解压 `/logs/trace_<case_id>_<ts>.zip`，用 Perfetto/chrome 打开，搜索 `silu_and_mul` 或 `rms_norm_kernel`。

**需要观察的现象**：终端先打印 `===== FILTERED PROFILER RESULTS =====` 表，列出 TileGym 内核的 CUDA 时间；CSV 里 `Filtered=True` 的行应与这些名字一致。

**预期结果**：开启 `--use_attn` 后，能在过滤结果里看到 attention 相关 kernel（如 `prefill_fmha`/`fmha_kernel` 一类）；关闭 `--use_attn` 则看不到。（具体名字待本地验证，依编译后 demangled 名为准。）

#### 4.3.5 小练习与答案

**练习 1**：`run_torch_profiler` 里 `cudaProfilerStart()/Stop()` 那段，在不跑 nsys 时有副作用吗？

**答案**：基本没有。这两个调用只是通知 CUDA 运行时「进入/退出可采集区间」，没有 nsys 监听时不会录像、开销极小；它的存在是为「同一份代码在 nsys 下也能正确划定捕获区间」做的双用途设计。

**练习 2**：torch profiler 路径里也用了 `KernelFilter`，但它和 coverage 路径里的 `KernelFilter` 是同一个类吗？为何要复用？

**答案**：是同一个 `KernelFilter`（都 import 自 `tilegym_hf_bench.kernel_filters`）。复用保证了「torch profiler 的 `Filtered=True`」与「nsys coverage 的命中口径」完全一致——同一份 YAML 规则统一了两条路径的判定标准。

---

### 4.4 kernel coverage 报告：nsys 双进程架构（`--report_kernel_coverage`）

#### 4.4.1 概念说明

`--report_kernel_coverage` 是本子项目的「招牌功能」。它回答：**一次推理里，GPU 花在 TileGym/cuTile 内核上的时间，占全部 GPU kernel 时间的百分之几？** 这个比值叫 **kernel coverage（内核覆盖率）**，是衡量「monkey-patch 到底替换得彻不彻底」的最客观指标——因为它不靠应用层日志，而靠 nsys 从硬件层读到的、真真切切执行过的每个 kernel 的纳秒级时间戳。

设计上的核心难点是：**nsys 必须从「外部」包裹目标进程**（`nsys profile -- python ...`），而 CLI 自己又是被 `python` 启动的。于是采用「双进程自递归」架构：外层进程发现 `--report_kernel_coverage`，**剥掉这个标志**，再用 `nsys profile -- python -m tilegym_hf_bench._cli <其余参数>` 启动一个子进程；子进程因标志被剥、不再递归，正常跑推理，全程被 nsys 录像。录像结束后，外层进程读 `.nsys-rep` → 导出 SQLite → 用 SQL 查询每个 kernel → 用 `KernelFilter` 分类 → 算两个比值。

#### 4.4.2 核心流程

```
外层 main():
  if args.report_kernel_coverage:
      NsysKernelCoverageReporter(args, raw_argv).run()
      return
        │
        ▼
NsysKernelCoverageReporter.run():
  ├─ _build_inner_args(): 从 argv 删掉 --report_kernel_coverage（防递归），并强制塞入 --profile（让子进程触发 cudaProfilerStart/Stop）
  ├─ _build_nsys_command(): ["nsys","profile","-c","cudaProfilerApi",
  │                           "--capture-range-end=stop-shutdown","--trace=cuda",
  │                           "-o",out,"--force-overwrite=true","--",
  │                           sys.executable,"-m","tilegym_hf_bench._cli"] + inner_args
  ├─ subprocess.Popen(...) 跑子进程，逐行回显输出
  ├─ _find_nsys_report(): glob out*.nsys-rep
  └─ _compute_and_report_ratio(rep):
        ├─ _extract_kernel_durations(): 连 SQLite，SELECT start,end,name FROM CUPTI_ACTIVITY_KIND_KERNEL
        ├─ _classify_kernels(): 用 KernelFilter.contains 分成 tilegym / other 两组
        └─ 算两个比值并 _print_report()
```

两个覆盖率指标的数学定义（`dur` 为单次 kernel 的 GPU 执行时长，单位纳秒）：

\[
\text{coverage}_{\text{time}} = \frac{\displaystyle\sum_{k\in \text{matched}} \text{dur}(k)}{\displaystyle\sum_{k\in \text{all}} \text{dur}(k)} \times 100\%
\]

\[
\text{coverage}_{\text{count}} = \frac{|\{k\in \text{matched}\}|}{|\{k\in \text{all}\}|} \times 100\%
\]

其中 `matched` 是「demangled 名命中 `KernelFilter`」的 kernel 集合。时间占比反映「算力落点」，次数占比反映「调用结构」。两者通常不一样：一个 `matmul` 可能次数少但耗时长。

#### 4.4.3 源码精读

**外层入口：发现 coverage 标志即转交并 return**：

[_cli.py:110-112](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/modeling/transformers/src/tilegym_hf_bench/_cli.py#L110-L112)：`main` 开头就检查 `report_kernel_coverage`，转交后立即 `return`——外层进程**不加载模型**，模型在子进程里加载。

**「剥标志 + 强制 profile」的内层参数构造**：

[nsys.py:44-56](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/modeling/transformers/src/tilegym_hf_bench/profiling/nsys.py#L44-L56)：遍历 `argv` 跳过 `--report_kernel_coverage`（否则子进程又进 coverage 分支 → 无限递归地套娃 nsys）；并保证 `--profile` 在内层参数里——这一步是必须的，因为 nsys 用 `-c cudaProfilerApi`，需要子进程里 `run_torch_profiler` 的 `cudaProfilerStart/Stop`（见 4.3.3）来开/关捕获区间，否则 nsys 抓不到任何东西。

**nsys 命令拼装**：

[nsys.py:63-79](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/modeling/transformers/src/tilegym_hf_bench/profiling/nsys.py#L63-L79)：`-c cudaProfilerApi`（API 触发捕获）、`--capture-range-end=stop-shutdown`（收到 stop 后再 shutdown 收尾）、`--trace=cuda`（只录 CUDA，省体积）、`-o output_base`（输出路径）、`--` 后接真正要跑的 `python -m tilegym_hf_bench._cli`。

**子进程跑完，找报告文件**：

[nsys.py:81-98](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/modeling/transformers/src/tilegym_hf_bench/profiling/nsys.py#L81-L98)：用 glob 匹配 `out*.nsys-rep`，按返回码判断成败；失败且无报告则带码退出。

**从 .nsys-rep 到 SQLite**（必要时自动 `nsys export`）：

[nsys.py:100-127](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/modeling/transformers/src/tilegym_hf_bench/profiling/nsys.py#L100-L127)：若只给了 `.nsys-rep`，就找同名 `.sqlite`；找不到则调 `nsys export --type=sqlite` 现导一份。把「读报告」统一成「读 SQLite」。

**用 SQL 读出每个 kernel 的起止与名字**：

[nsys.py:129-148](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/modeling/transformers/src/tilegym_hf_bench/profiling/nsys.py#L129-L148)：`SELECT k.start, k.end, s.value AS name FROM CUPTI_ACTIVITY_KIND_KERNEL k JOIN StringIds s ON k.demangledName = s.id`。注意取的是 **demangledName**（可读名），这正是 `KernelFilter` 的子串能命中的前提；时长 = `end - start`（纳秒）。

**分类**：

[nsys.py:150-162](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/modeling/transformers/src/tilegym_hf_bench/profiling/nsys.py#L150-L162)：逐个 kernel 调 `kernel_filter.contains(name)`，命中进 `tilegym_*` 字典，否则进 `other_*`，分别累加时长与次数。

**算比值 + 打印报告**：

[nsys.py:164-194](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/modeling/transformers/src/tilegym_hf_bench/profiling/nsys.py#L164-L194)：`tilegym_total_ns / all_total_ns * 100` 与 `tilegym_total_count / all_total_count * 100`，即上面的两个公式；`all_* = tilegym_* + other_*`。

**最终两行醒目输出**：

[nsys.py:228-229](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/modeling/transformers/src/tilegym_hf_bench/profiling/nsys.py#L228-L229)：`>>> cuTile Kernel Coverage (GPU Time): xx.xx% <<<` 与 `>>> cuTile Kernel Coverage (# Launches): xx.xx% <<<`。

#### 4.4.4 代码实践

**实践目标**：写出对 Llama-3.1-8B 开启 TileGym 注意力并报告覆盖率的命令，并解释 coverage 由 `kernel_filters` 如何定义。

**操作步骤**：

1. 进入子项目并同步依赖（待本地验证）：
   ```bash
   cd modeling/transformers
   uv sync --locked
   ```
2. 运行覆盖率命令（这正是 README「Kernel Coverage」一节的示例）：
   ```bash
   uv run tilegym-hf-bench \
       --model_id meta-llama/Meta-Llama-3.1-8B \
       --use_tilegym \
       --use_cutile \
       --use_attn \
       --report_kernel_coverage \
       --sentence_file sample_inputs/input_prompt_32K.txt \
       --output_length 100
   ```
3. 在终端先看到外层打印的 `Running nsys profile command: ...`，其中 `--` 之后是 `python -m tilegym_hf_bench._ci` + 内层参数（注意 `--report_kernel_coverage` 已被剥掉、`--profile` 已被塞入）。
4. 子进程跑完后，看到 `===== NSYS KERNEL GPU TIME ANALYSIS =====` 表，底部两行 `>>> cuTile Kernel Coverage ... <<<`。

**需要观察的现象**：表里逐行列出每个命中 kernel 的 `# Calls`、`GPU Time (ms)`、`% of Total`；最后给出时间占比与次数占比两个汇总数。

**预期结果**：开启 `--use_attn` 后，attention 子串（`prefill_fmha`/`fmha_kernel` 等）应出现在命中表里，时间占比相比「不开 attention」明显升高；具体数值待本地验证（取决于模型、输入长度、硬件）。

**说明 coverage 指标如何由 kernel_filters 定义**：coverage 的分子，是所有「demangled 名**包含** `tilegym_kernel_prefixes.yaml` 里任一 `prefixes` 子串、且**整串不在** `blacklist`」的 kernel（见 [filters.py:24-28](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/modeling/transformers/src/tilegym_hf_bench/kernel_filters/filters.py#L24-L28)）的 GPU 时间之和；分母是**全部** kernel 的 GPU 时间之和（见 [nsys.py:188-189](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/modeling/transformers/src/tilegym_hf_bench/profiling/nsys.py#L188-L189)）。因此「YAML 改一行，覆盖率数字就跟着变」——例如往 `blacklist` 加一个名字，该名字的耗时立刻从分子里剔除；往 `prefixes` 加一个新子串，新匹配上的 kernel 立刻算进分子。所以覆盖率既是「工程指标」也是「配置约定」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `_build_inner_args` 要把 `--report_kernel_coverage` 删掉？不删会怎样？

**答案**：子进程是用 `python -m tilegym_hf_bench._cli <内层参数>` 启动的。若内层参数里仍带 `--report_kernel_coverage`，子进程的 `main` 又会进入 coverage 分支，再 fork 一个 nsys 套在自己外面……形成无限递归地套娃。删掉它，子进程就走正常推理 + `--profile` 路径，被外层的 nsys 录像。

**练习 2**：把覆盖率从「按时间」改成「按 launch 次数」，分母分子分别对应代码里的哪些量？

**答案**：分母是 `all_total_count`（所有 kernel 调用次数之和），分子是 `tilegym_total_count`（命中 `KernelFilter` 的 kernel 调用次数之和），二者都在 [nsys.py:175-177](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/modeling/transformers/src/tilegym_hf_bench/profiling/nsys.py#L175-L177) 算出，比值见 [nsys.py:189](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/modeling/transformers/src/tilegym_hf_bench/profiling/nsys.py#L189)。

**练习 3**：如果某次 coverage 显示「GPU 时间占比 95%，但 launch 次数占比只有 30%」，这说明什么？

**答案**：说明 TileGym 用**少量大 kernel**（如融合 matmul / attention）吃掉了绝大部分算力，而剩余 70% 的 launch 是大量小而短的原生 kernel（如 reshape/copy/elementwise 的 aten op）。这通常是「主干已替换、零碎 op 未融合」的典型画像，可作为下一步优化（继续融合那些小 op）的依据。

---

## 5. 综合实践

把本讲四块知识串成一个完整的「基准 + 诊断」流程。目标：对同一个模型，比较「naive」与「TileGym 全开」两种配置，并用 coverage 解释差异来源。

**任务步骤**（待本地验证，需 GPU 与模型缓存）：

1. 用预设脚本一键跑三轮（baseline / cutile / coverage），它会自动写 summary：
   ```bash
   cd modeling/transformers
   ./scripts/benchmark_hf_model.sh --model-key llama
   ```
2. 打开生成的 `llama_benchmark_summary.txt`，对比 `..._naive_bf16` 与 `..._cutile_attn_bf16` 两行的 `tokens/sec`。
3. 查看 coverage 输出（脚本末尾自动跑），记下 `cuTile Kernel Coverage (GPU Time)` 与 `(# Launches)` 两个百分比。
4. **诊断**：若 tokens/sec 提升明显但 coverage 时间占比偏低（比如 < 60%），打开 `/logs` 下对应 `profiler_results_*.csv`，按 `Filtered=False` 且 `CUDA_time_total_us` 降序，找出「最耗时的非 TileGym kernel」，判断它是否是下一个值得融合/替换的目标。
5. **改规则看变化**：在 `tilegym_kernel_prefixes.yaml` 的 `blacklist` 临时加一个误命中的名字，重跑 coverage，观察时间占比下降——以此亲手验证「coverage 数字直接受 YAML 规则控制」。

**预期结果**：你能用一句话解释「提速来自哪里、还剩多少没替换」，这正是 kernel coverage 作为诊断工具的价值。

## 6. 本讲小结

- `tilegym-hf-bench` 是 `modeling/transformers` 这个 uv 子项目提供的 CLI；`infer.py` 只是兼容垫片，三者（控制台脚本 / `-m` 模块 / `infer.py`）最终都汇入 `_cli.py:main`。
- 三个布尔开关 `--use_tilegym` / `--use_cutile` / `--use_attn` 决定是否打补丁、是否切 cuTile 后端、是否替换注意力；patch 必须早于模型实例化。
- `KernelFilter` 是 torch profiler 与 nsys 两条路径共用的「判官」，规则是 YAML 里的 `prefixes`（子串包含，非真前缀）+ `blacklist`（整串相等）；`aten::matmul` 黑名单是子串误伤的活证据。
- `--profile` 走 `torch.profiler`，产出 CSV 与 Chrome trace；其 `cudaProfilerStart/Stop` 一段兼作 nsys 的捕获开关。
- `--report_kernel_coverage` 采用「外层剥标志 + nsys 包子进程」的双进程自递归架构，读 `.nsys-rep` → SQLite → SQL 查 `CUPTI_ACTIVITY_KIND_KERNEL` → 分类 → 算时间占比与次数占比两个比值。
- 覆盖率既是工程指标也是配置约定：改 YAML 一行，分子立刻跟着变。

## 7. 下一步学习建议

- **回到内核侧**：coverage 报告里出现的每个命中 kernel 名（`prefill_fmha`、`static_persistent_matmul_kernel`、`rms_norm_kernel` 等），其实现正是 u3–u6 讲过的 cuTile 内核。带着 coverage 的「耗时排行」去重读对应内核，能有的放矢地优化。
- **学测试与贡献流程（u9）**：若你想让某个新算子也出现在 coverage 里，需要同时改两处——在内核侧 `@register_impl` 注册（u9-l2），并在 `tilegym_kernel_prefixes.yaml` 加上它的名字子串。
- **进阶生态（u10）**：本讲的 coverage 思路（基于 kernel 名做覆盖统计）与 u10-l2 的 `kernel_inventory`（基于 pydantic 数据契约生成内核清单）是互补的两套「内核可观测性」工具，可对比阅读。
- **进一步 profiling**：若想看单 kernel 的寄存器/占用率（而非只看时间），可从 nsys（系统级）升级到 `ncu`（Nsight Compute，kernel 级），那已超出本讲范围。
