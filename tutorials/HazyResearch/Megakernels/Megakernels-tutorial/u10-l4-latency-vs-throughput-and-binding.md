# latency vs throughput 与 pybind11 绑定

> 阶段：advanced · 依赖：[u2-l1]（三种执行模式与 dispatch 的关系）、[u9-l3]（低延迟 Llama 的 op 流水线与 `globals_t` 字段）。建议你已经读过 `dispatch.py` 在 `torch/pyvm/mk` 三模式里扮演的角色，也大致看过 `llama.cuh` 里 `globals_t` 的字段布局，再进入本讲。

## 1. 本讲目标

本讲解决两个看起来无关、其实是同一件事两端的问题：

- **Python 这一端**：同一个 `mode=mk`，凭什么能跑「低延迟」和「高吞吐」两套完全不同的内核？[dispatch.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/dispatch.py) 是怎么用 `setting=latency/throughput` 切换 `ScheduleBuilder` 与 `MK_Interpreter` 的？
- **C++ 这一端**：一个用 CUDA 写的 megakernel，是怎么变成 Python 里一个可直接 `import`、可直接调用的函数的？[llama.cu](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cu) 里的 `kittens::py::bind_kernel` 又是怎么把内核连同它的 `globals` 字段「绑定」到 Python 的？

学完本讲，你应当能够：

1. 画清 [dispatch.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/dispatch.py) 的三张映射表（`BUILDER_MAP` / `MK_INTERPRETER_MAP` / `INSTRUCTION_TO_SOLVER_MAP`）与三个工厂函数的关系，并指出一个易混淆的命名点：这里的参数名叫 `mode`，实际收到的却是 `config.setting`。
2. 逐项说清「低延迟版 `LatencyMK_Interpreter`」与「高吞吐版 `ThroughputMK_Interpreter`」传给 `mk_func` 的参数差异——尤其是 `throughput` 多出的 `batch_size`、几个 `rms_*_intermediates` 激活缓冲，以及 `latency` 独有的 `skip_attn_reduction` 与 `stream=`。
3. 讲透 `kittens::py::bind_kernel` 的「成员指针变元表」契约：`PYBIND11_MODULE` 里那串 `&llama_1b_globals::xxx` 的**顺序**，如何与 [latency/mk.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/mk.py) 里 `mk_func(globs.barriers, globs.instructions, ...)` 的**位置**一一对应。

---

## 2. 前置知识

先用大白话对齐几个概念，细节都在依赖讲义里。

**工厂（factory）是什么？**

「工厂」就是「给我一个 key，我返回给你一个合适的对象」。`dispatch.py` 把「latency」「throughput」这两个字符串当成 key，查表返回对应的调度器类、解释器类、solver 表。这样 [generate.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py) 只需要调 `make_mk_interpreter(config.setting, ...)`，不用关心背后是哪一族实现。

**`setting` 和 `mode` 是两件事，别混了。**

这是本讲最容易踩的坑，必须先讲清：

| 名字 | 取值 | 决定什么 | 在哪里传 |
| --- | --- | --- | --- |
| `mode` | `torch` / `pyvm` / `mk` | **执行器**（PyTorch 模型 / Python 虚拟机 / CUDA megakernel） | `generate.py` 的 `config.mode`，由 `match` 分支选 `Generator` |
| `setting` | `latency` / `throughput` | **实现族**（低延迟版 vs 高吞吐版的调度器/解释器/solver） | `generate.py` 的 `config.setting`，传给 `dispatch.py` |

换句话说：`mode` 选「用什么算」，`setting` 选「算的这套是低延迟优化还是高吞吐优化」。它们是正交的两个维度，组合出 `mk`+`latency`、`mk`+`throughput`、`pyvm`+`latency` ……等路径。详见 [u2-l1] 的 4.5 节。

> 命名陷阱：`dispatch.py` 三个工厂函数的参数都叫 `mode`，但 `generate.py` 传进去的是 `config.setting`（[generate.py:139](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py#L139)、[generate.py:150](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py#L150)、[generate.py:153](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py#L153)）。本讲里我会把 `dispatch.py` 的形参 `mode` 读作「实现族 setting」，避免和 `config.mode` 混淆。

**pybind11 是什么？**

[pybind11](https://github.com/pybind/pybind11) 是一个把 C++ 代码「暴露成 Python 模块」的库。你写一个 `PYBIND11_MODULE(模块名, m) { ... }` 宏块，编译成动态库（`.so`），Python 里 `import 模块名` 就能用里面注册过的函数/类。Megakernels 在 `make` 时，正是用 pybind11 把 `llama.cu` 编成可被 `import mk_llama` 的扩展（见 [Makefile:17](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/Makefile#L17) 里 `$(shell python3 -m pybind11 --includes)`）。

**C++ 成员指针（pointer-to-member）是什么？**

`&llama_1b_globals::qkv_weights` 这个写法是「指向 `llama_1b_globals` 这个结构体里 `qkv_weights` 成员的指针」。它本身不是地址里的值，而是「这个成员在结构体里的相对位置」。给它一个具体的结构体实例 `g`，用 `g.*ptr` 就能取出 `g.qkv_weights`。`bind_kernel` 正是用一连串成员指针，告诉 pybind11「Python 传进来的第 i 个参数，请绑到 globals 的这个成员上」。

**低延迟 vs 高吞吐，到底在优化什么？**

- **latency（低延迟）**：目标是「单个请求、单条序列，越快返回越好」。典型 batch=1。代价是算力利用率低（GPU 大部分 SM 闲着也在等），换来的是极短的首 token 延迟。低延迟 demo 的 attention 被拆成 `partial` + `reduction` 跨 SM 并行（见 [u9-l3]）。
- **throughput（高吞吐）**：目标是「一批请求一起算，每秒处理的总 token 越多越好」。典型 batch=1024。每个 op 变成「批矩阵乘（batched matmul）」，attention 变成朴素的逐 head 解码（`AttentionDecode`，不再 partial/reduction 拆分），把 SM 喂满。

这套差异会直接体现在「解释器传给内核的参数」上，是本讲 4.2 的重点。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [megakernels/dispatch.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/dispatch.py) | **模块 4.1 主角**。三张映射表 + 三个工厂函数，按 `setting` 选 builder / mk 解释器 / solver 表。 |
| [megakernels/demos/throughput/mk.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/throughput/mk.py) | **模块 4.2 主角（高吞吐侧）**。`ThroughputMK_Interpreter` 把 `globs` 一口气喂给 `mk_func`，参数顺序与字段都按吞吐场景裁剪。 |
| [megakernels/demos/latency/mk.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/mk.py) | **模块 4.2 对照（低延迟侧）**。`LatencyMK_Interpreter`，与吞吐版逐参数对比的基准。 |
| [demos/low-latency-llama/llama.cu](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cu) | **模块 4.3 主角**。`PYBIND11_MODULE` + `kittens::py::bind_kernel`，把低延迟内核与 `globals` 字段暴露成 Python 函数。 |
| [demos/low-latency-llama/llama.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh) | `globals_t`：`bind_kernel` 里每个成员指针指向的字段都在这里声明。 |
| [megakernels/mk.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/mk.py) | `MK_Interpreter` 基类：`get_mk_func` 动态 `import mk_llama`，把 `mk_dir` 变成一个可调用对象。 |
| [megakernels/demos/throughput/instructions.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/throughput/instructions.py) | 吞吐场景的 `Globals` dataclass，解释了吞吐 `mk_func` 为什么多出 `batch_size`、`rms_*_intermediates` 等字段。 |
| [megakernels/demos/latency/instructions.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/instructions.py) | 低延迟场景的 `Globals` dataclass，含 `skip_attn_reduction`、`attn_*_intermediates`。 |
| [megakernels/instructions.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/instructions.py) | `BaseGlobals`：latency/throughput 共享的权重/常量字段都在这里。 |

> 注：`kittens::py::bind_kernel` 的实现位于 ThunderKittens 子模块的 `pyutils/pyutils.cuh`（由 `llama.cu:10` 的 `#include "pyutils/pyutils.cuh"` 引入，子模块见 `.gitmodules`），本仓库快照未检出该子模块。本讲依据**两处调用点**（[llama.cu:28-52](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cu#L28-L52) 与项目模板 `util/mk_init/.../{{PROJECT_NAME_LOWER}}.cu`）描述其**对外契约**，不臆测其内部实现。

---

## 4. 核心概念与源码讲解

### 4.1 dispatch 工厂映射：用 setting 选 builder / interpreter / solver 三件套

#### 4.1.1 概念说明

[u2-l1] 讲过，`mk`/`pyvm` 两种执行器都要消费两样东西：

1. **调度器（`ScheduleBuilder`）**：把 PyTorch 模型「编译」成一串指令 + 一份 `globs`。
2. **解释器（`MK_Interpreter` 或 `PyVM_Interpreter`）**：拿这份 `globs` 去执行——`mk` 解释器调 CUDA 内核，`pyvm` 解释器逐条调 PyTorch solver。

而「低延迟」和「高吞吐」**各自有一整套不同的调度器与解释器实现**：指令集不同（latency 7 条 opcode、throughput 10 条 opcode，见 4.2.3）、执行结构不同（matvec vs batched matmul、partial-attention vs decode-attention）。如果把它们都硬编码进 `generate.py`，脚本会塞满 `if setting == "latency"` 分支，难以维护。

`dispatch.py` 用「**三张表 + 三个工厂**」把这件事收口：它把每个 `setting` 对应的「调度器类、mk 解释器类、solver 表」分别登记在三张字典里，再用三个薄薄的工厂函数封装「查表 + 构造」。`generate.py` 只认工厂函数，完全不感知具体类。

#### 4.1.2 核心流程

```
generate.py 传 config.setting (latency/throughput) 给 dispatch.py 的工厂
                │
                ├── make_schedule_builder(setting)   ─查 BUILDER_MAP─► Latency/Throughput ScheduleBuilder
                ├── make_mk_interpreter(setting, mk_dir) ─查 MK_INTERPRETER_MAP─► Latency/Throughput MK_Interpreter
                └── make_pyvm_interpreter(setting)   ─查 INSTRUCTION_TO_SOLVER_MAP─► PyVM_Interpreter(该族 solver 表)
```

三个工厂的查表逻辑都极简：`return MAP[mode]()`（或带一个构造参数）。`dispatch.py` 整个文件只有这几行实质逻辑，是典型的「**用字典当分支表**」写法——比一长串 `if/elif` 更适合「key 多、每个分支只是选一个对象」的场景。

#### 4.1.3 源码精读

**导入：把两族的实现都拉进来**（[dispatch.py:1-15](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/dispatch.py#L1-L15)）——文件顶部从 `demos.latency.*` 与 `demos.throughput.*` 各导入一套 `ScheduleBuilder`、`MK_Interpreter`、`INSTRUCTION_TO_SOLVER`，分别起别名（如 `LATENCY_INSTRUCTION_TO_SOLVER` / `THROUGHPUT_INSTRUCTION_TO_SOLVER`），避免命名冲突。这一步决定了「latency 和 throughput 是两套平行实现」。

**三张映射表**（[dispatch.py:17-30](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/dispatch.py#L17-L30)）：

| 表 | latency → | throughput → | 产出 |
| --- | --- | --- | --- |
| `BUILDER_MAP` | `LatencyScheduleBuilder` | `ThroughputScheduleBuilder` | 调度器实例 |
| `MK_INTERPRETER_MAP` | `LatencyMK_Interpreter` | `ThroughputMK_Interpreter` | `mk` 解释器类 |
| `INSTRUCTION_TO_SOLVER_MAP` | `LATENCY_INSTRUCTION_TO_SOLVER` | `THROUGHPUT_INSTRUCTION_TO_SOLVER` | `pyvm` 的 solver 字典 |

注意 `MK_INTERPRETER_MAP` 存的是**类**（要 `(...)` 实例化，且要传 `mk_dir`），而 `INSTRUCTION_TO_SOLVER_MAP` 存的是**现成的字典**（直接塞给 `PyVM_Interpreter` 构造）。这个区别会被下面的工厂函数体现出来。

**三个工厂函数**（[dispatch.py:33-42](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/dispatch.py#L33-L42)）：

```python
def make_schedule_builder(mode: str) -> ScheduleBuilder:
    return BUILDER_MAP[mode]()                      # 无参构造

def make_mk_interpreter(mode: str, mk_dir: Path) -> MK_Interpreter:
    return MK_INTERPRETER_MAP[mode](mk_dir)         # 要传 mk_dir（指向编译产物 mk_llama）

def make_pyvm_interpreter(mode: str) -> PyVM_Interpreter:
    return PyVM_Interpreter(INSTRUCTION_TO_SOLVER_MAP[mode])   # 把该族 solver 表喂进去
```

- `make_mk_interpreter` 多收一个 `mk_dir`，是因为 `mk` 解释器要**动态 `import` 编译好的 CUDA 扩展**（`from mk_llama import mk_llama`，见 [mk.py:5-9](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/mk.py#L5-L9)），而扩展在哪取决于你编了哪个 demo。
- `make_pyvm_interpreter` 不需要路径，因为 solver 是纯 Python 函数；但它需要知道「用哪一族指令的 solver 表」（latency 与 throughput 的指令类型不同，solver 也不同）。

**谁在调这些工厂**（[generate.py:139-153](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py#L139-L153)）——`config.setting` 同时驱动三处：第 139 行建调度器、第 150 行建 `pyvm` 解释器、第 153 行建 `mk` 解释器。注意第 139 行的调度器**对所有 mode 都建**（包括用不到它的 `torch`），这是 [u2-l1] 4.5 讲过的「保证三模式跑在同一份调度上便于对拍」的设计。

**throughput 场景的额外配置**（[generate.py:59-75](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py#L59-L75)）——配置对象有个 `th()` 方法（throughput 预设）：它把 `setting` 改成 `"throughput"`、把 `mk_dir` 指向 `tests/batch-vm/llama_official`（吞吐内核的编译目录）、`batch_size=1024`、`interleave_rope=False`，并切到 8B 模型（`self.l8()`）。第 72-75 行还有一条断言：

```
if self.mode == "mk":
    assert self.batch_size == 1024, "must recompile the kernel with new BATCH_SIZE"
```

这说明**吞吐内核在编译期就把 `BATCH_SIZE` 写死成了 1024**（batched matmul 的批维度是模板常量），要换 batch 就得重编。这和低延迟内核（batch 恒为 1、`batch_size` 不进模板）形成鲜明对比——也是 4.2 里 `throughput` 多传一个 `batch_size` 标量的根因。

#### 4.1.4 代码实践

**目标**：亲手确认「换 `setting` = 换一整套实现族」，且不改 `generate.py` 的主流程。

1. 打开 [dispatch.py:17-30](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/dispatch.py#L17-L30)，在三张表里各找一项，验证：`setting="latency"` 时 `mk` 走 `LatencyMK_Interpreter`、`setting="throughput"` 时走 `ThroughputMK_Interpreter`，两者**来自不同的模块**（`demos.latency.mk` vs `demos.throughput.mk`）。
2. 阅读工厂函数 [dispatch.py:33-42](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/dispatch.py#L33-L42)，确认三个工厂都只是 `return MAP[mode](...)`，没有任何 if 分支——「分支」被吸收进了字典。
3. 想象新增一个 `setting="balanced"`：你只需在每张表里加一行 `"balanced": XxxClass`，`generate.py` 一行都不用改。这就是「字典当分支表」的可扩展性。

**预期结果**：你会清楚看到，`mode`（torch/pyvm/mk）和 `setting`（latency/throughput）是两个正交开关；dispatch.py 只管 `setting` 这一个维度，且实现方式是纯查表。

> 本地是否运行：纯源码阅读，无需 GPU。

#### 4.1.5 小练习与答案

**Q1**：`dispatch.py` 三个工厂的形参都叫 `mode`，但 `generate.py` 传的是 `config.setting`。为什么不直接把形参也叫 `setting`？这样做有什么隐患？

**答**：这纯粹是历史命名遗留。隐患就是本讲开篇指出的「命名陷阱」——读者很容易把它和 `config.mode`（torch/pyvm/mk）混淆，以为 dispatch 在选执行器。实际它选的是「实现族」。好的改法是把形参重命名为 `setting`，但那需要同步改三个函数签名与所有调用点。

**Q2**：为什么 `MK_INTERPRETER_MAP` 存「类」、`INSTRUCTION_TO_SOLVER_MAP` 存「现成字典」？

**答**：`mk` 解释器实例化时需要 `mk_dir`（运行时才知道编译产物在哪），所以必须存类、到工厂函数里再带参 `(...)` 构造。`pyvm` 的 solver 表是一组静态映射好的 Python 函数，不依赖运行时路径，构造时就绪，所以直接存字典、`PyVM_Interpreter(字典)` 包一层即可。

---

### 4.2 throughput mk 参数差异：与 latency 的 mk_func 逐项对比

#### 4.2.1 概念说明

无论是低延迟还是高吞吐，`mk` 模式的本质都是「**一次函数调用把整个 `globs` 喂给 GPU megakernel**」。这发生在各自的 `interpret_with_mk` 函数里：

```python
mk_func(
    globs.barriers, globs.instructions, globs.timings,   # vm stuff
    ...各种权重..., fourD_k_cache, fourD_v_cache,         # weights + kv cache
    globs.rope_cos, globs.rope_sin,                       # rope
    ...各种激活...,                                         # activations
    ...各种标量...,                                         # scalars
)
```

骨架一模一样，但**两边传的具体字段不同**。这些差异不是随便填的，而是两套内核（低延迟版 vs 高吞吐版）**内部需要/不需要哪些缓冲**的直接反映。逐项对比这两份参数表，就能从 Python 侧「读出」两套内核的架构差别。这正是本讲核心实践任务之一。

#### 4.2.2 核心流程：两份参数表的逐项对齐

把 [latency/mk.py:15-49](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/mk.py#L15-L49) 与 [throughput/mk.py:14-49](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/throughput/mk.py#L14-L49) 并排，按位置对齐（同名只是 Python/C++ 命名风格不同的，标注「等价」）：

| 位置 | latency（低延迟） | throughput（高吞吐） | 说明 |
| --- | --- | --- | --- |
| 1–3 | `barriers` / `instructions` / `timings` | `barriers` / `instructions` / `timings` | **vm 三件套，完全相同** |
| 4–10 | 7 个权重（qkv/attn_ln/o/mlp_ln/up/gate/down） | 同左 | 权重前 7 个相同 |
| 11–12 | `lm_head_norm_weights.data` / `lm_head_weights.data` | 同左 | lm_head 权重相同 |
| 13–14 | `fourD_k_cache` / `fourD_v_cache` | 同左（都先 `rearrange` 成 4D） | KV cache 形状约定相同 |
| 15–16 | `rope_cos` / `rope_sin` | 同左 | rope 表相同 |
| 17 | `hidden_states` | `hidden_states` | 主激活相同 |
| 18+ | **`post_ln_rope_q`** | **`rms_rope_intermediates`** | 分歧开始（见下） |

分歧集中在「激活缓冲（position 18 起）」和「标量（末尾）」两段。下面分别拆解。

**(A) 激活缓冲的差异：attention 的两种实现**

- **latency**（[latency/mk.py:36-42](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/mk.py#L36-L42)）传了一组「**partial + reduction**」中间量：
  - `post_ln_rope_q`、`attn_out`、**`attn_lse_intermediates`**、**`attn_out_intermediates`**、`silu_out`、`logits`。
  - `attn_lse_intermediates` / `attn_out_intermediates` 是因为低延迟 attention 被拆成「多个 SM 各算一段 partial（产出局部 \(O\) 与 LSE）→ `attention_reduction` 用 LSE 合并」（见 [u9-l3] 4.2）。所以需要这些跨 SM 的中间缓冲。
- **throughput**（[throughput/mk.py:34-44](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/throughput/mk.py#L34-L44)）没有这些 partial 缓冲，反而多了一组「**RMS 中间量**」：
  - `rms_rope_intermediates`、`rms_gate_intermediates`、`silu_out`、`post_ln_rope_q`、`attn_out`、`silu_out`、**`rms_lm_head_intermediates`**、`logits`。
  - 高吞吐 attention 是朴素的 `AttentionDecode`（每个 kv head 一条指令，一次解码出全序列，不拆 partial），所以不需要 lse/out 中间量；但它的 RMSNorm 走的是批处理路径，把 RMS 缩放中间结果显式留成缓冲（`rms_rope_intermediates` 等），供后续 batched matmul 复用。

> 这些字段都能在各自的 `Globals` dataclass 里找到出处：latency 的 `attn_lse_intermediates`/`attn_out_intermediates`/`skip_attn_reduction` 在 [latency/instructions.py:13-19](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/instructions.py#L13-L19)；throughput 的 `rms_rope_intermediates`/`rms_gate_intermediates`/`rms_lm_head_intermediates`/`batch_size` 在 [throughput/instructions.py:11-21](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/throughput/instructions.py#L11-L21)。

**(B) 标量的差异：`batch_size` 与 `skip_attn_reduction`**

- **throughput 末尾多传 `globs.batch_size`**（[throughput/mk.py:48](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/throughput/mk.py#L48)）：因为吞吐内核是 batched matmul，批维度大小是运行时参数（即使编译期 `BATCH_SIZE=1024` 写死，内核仍接收它做边界/分块计算）。
- **latency 末尾多传 `globs.skip_attn_reduction`**（[latency/mk.py:47](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/mk.py#L47)）：这是一个**调试开关**（`bool`），让你跳过 attention 的 reduction 阶段、只跑 partial，用来单独压测/定位 partial 是否正确（呼应 [u2-l1] 4.4 的分层调试思想）。高吞吐 attention 没有独立 reduction 阶段，自然不需要它。

**(C) `stream=` 的差异**

- **latency 显式传 `stream=torch.cuda.current_stream()`**（[latency/mk.py:48](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/mk.py#L48)），throughput 没传。这是 `bind_kernel` 暴露的可选关键字参数（见 4.3.2）：指定内核在哪个 CUDA stream 上 launch。latency 对 stream 显式控制（便于和 host 侧计时、嵌入流程精确同步）；throughput 走默认。

#### 4.2.3 为什么会有这些差异：两套指令集的对照

参数差异的根源是**两套不同的指令集（opcode 体系）**，它们决定了内核需要哪些缓冲：

| 维度 | latency（7 条 opcode） | throughput（10 条 opcode） |
| --- | --- | --- |
| matvec vs matmul | **matvec**（split-K，每 op 16 元素输出块） | **batched matmul**（整批一起算） |
| attention | `PartialAttention`(2) + `AttentionReduction`(3) 跨 SM 拆分 | `AttentionDecode`(3) 逐 kv head 一次解码，无 reduction |
| up/gate | `LayerNormDoubleMatVecSiLU`(5) 一个 op 算两路 | `GateSilu`(6) + `UpMatMul`(7) 拆成两个 op |
| 标量 | `skip_attn_reduction` 调试开关 | `batch_size` 批维度 |

（latency opcode 见 [latency/instructions.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/instructions.py)，throughput opcode 见 [throughput/instructions.py:57-223](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/throughput/instructions.py#L57-L223) 的各 `opcode()` 类方法。）

一句话总结：**latency 把计算拆细、跨 SM 并行，需要更多跨块中间量（partial/reduction）；throughput 把计算批量化、喂满 SM，需要批维度（batch_size）和批化的 RMS 中间量**。

#### 4.2.4 源码精读

**(a) `MK_Interpreter` 基类与动态加载**（[mk.py:1-17](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/mk.py#L1-L17)）——`get_mk_func` 把 `mk_dir` 加进 `sys.path` 再 `from mk_llama import mk_llama`（第 6-7 行），返回这个 pybind11 暴露的可调用对象。`MK_Interpreter.__init__` 把它存成 `self.mk_func`；`interpret` 留给子类实现。所以 `LatencyMK_Interpreter` 与 `ThroughputMK_Interpreter` 共享同一个 `mk_func` 加载机制，只差 `interpret` 里传什么参数。

**(b) KV cache 的 4D 重排是两边共同的**（[latency/mk.py:12-13](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/mk.py#L12-L13) 与 [throughput/mk.py:11-12](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/throughput/mk.py#L11-L12)）——两边都用 einops 把 `k_cache`/`v_cache` 从 `(l b t h d)` 重排成 `((l b) t h d)`（合并层与批维），因为内核侧 `kv_cache_t` 的第一维就是这个合并维（见 [llama.cuh:94-95](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/low-latency-llama/llama.cuh#L94-L95) 的 `kittens::gl<..., -1, -1, -1, head_dim, ...>`）。

**(c) throughput 里 `mk_func` 调用注释保留了一份「等价签名」**（[throughput/mk.py:51-79](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/throughput/mk.py#L51-L79)）——这段被注释掉的 `mk_llama(...)` 列出了每个位置参数的语义名（`Bar`、`instructions`、`timings`、`qkv_weights`……`logits`、`pos_id`、`attn_scale`、`rms_norm_eps`）。读它能帮你把 4.2.2 那张对齐表和实际位置一一钉死——注释里参数出现的顺序，就是 Python 传参的顺序，也就是下一节 `bind_kernel` 成员指针的顺序。

#### 4.2.5 代码实践

**目标**：把 `LatencyMK_Interpreter` 与 `ThroughputMK_Interpreter` 传给 `mk_func` 的参数逐项对比，并解释每个差异对应内核侧的什么需求。

**操作步骤（源码阅读 + 填表型）**：

1. 并排打开 [latency/mk.py:15-49](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/mk.py#L15-L49) 和 [throughput/mk.py:14-49](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/throughput/mk.py#L14-L49)，从第 1 个参数（`barriers`）数到最后一个，标出**第一次出现分歧的位置**（答案：第 18 个参数，latency 是 `post_ln_rope_q`，throughput 是 `rms_rope_intermediates`）。
2. 数两边**参数总数**：latency 共 27 个位置参数 + 1 个 `stream=` 关键字；throughput 共 29 个位置参数、无 `stream=`。
3. 对每个「只在一边出现」的字段，回查它的来源 dataclass 并写一句话理由：
   - `skip_attn_reduction`（仅 latency，[latency/instructions.py:19](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/instructions.py#L19)）→ 低延迟 attention 有可跳过的 reduction 阶段。
   - `batch_size`（仅 throughput，[throughput/instructions.py:21](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/throughput/instructions.py#L21)）→ 批矩阵乘的批维度。
   - `attn_lse_intermediates`/`attn_out_intermediates`（仅 latency）vs `rms_rope_intermediates`/`rms_gate_intermediates`/`rms_lm_head_intermediates`（仅 throughput）→ 见 4.2.2(A)。
4. 借助 [throughput/mk.py:51-79](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/throughput/mk.py#L51-L79) 的注释签名，确认你填的位置和注释里的语义名对得上。

**需要观察的现象**：两边的「vm 三件套 + 权重 + kv cache + rope」完全对齐；分歧只在「attention 相关激活」「rms 中间量」「末尾标量」三处——而这正是两套内核 attention 实现与批量化程度的差别。

**预期结果**：你能在不看答案的情况下，指着任意一个差异字段说出「这个缓冲/标量为什么这个内核需要、那个内核不需要」。

> 本地是否运行：纯源码阅读，无需 GPU。若想运行，throughput 内核需在 `tests/batch-vm/llama_official` 单独编译（且 `BATCH_SIZE=1024` 写死，见 4.1.3），本仓库快照未包含该生成目录，故此处以阅读型实践为主。

#### 4.2.6 小练习与答案

**Q1**：为什么 throughput 多传 `batch_size`，而 latency 完全没有这个参数？

**答**：throughput 内核是 batched matmul 解码器，批维度是它分块、寻址的核心参数（且编译期 `BATCH_SIZE=1024` 写死，见 [generate.py:72-75](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py#L72-L75) 的断言）。latency 内核恒为 batch=1，批维度隐含为 1，没必要作为运行时参数传入。

**Q2**：`LatencyMK_Interpreter` 传了 `attn_lse_intermediates` 和 `attn_out_intermediates`，`ThroughputMK_Interpreter` 没传。这反映了 attention 实现的什么差别？

**答**：latency 的 attention 被拆成跨 SM 的 `PartialAttention`（每个 partial 产出局部 \(O\) 和 LSE）+ `AttentionReduction`（用 LSE 合并），这两个缓冲正是存放各 partial 中间结果的。throughput 的 attention 是 `AttentionDecode`，每个 kv head 一条指令一次解码出完整结果，不存在跨 SM 的 partial 合并，所以不需要这两个中间量。

**Q3**：latency 传了 `stream=torch.cuda.current_stream()`，throughput 没传。不传时会怎样？

**答**：`stream=` 是 `bind_kernel` 暴露的**可选**关键字参数（见 4.3.2）。不传时，pybind11 绑定层会使用默认 stream（通常即当前 stream 或 legacy default stream）。latency 显式传是为了和 host 侧 CUDA event 计时精确对齐；throughput 不传意味着它接受默认行为。两种都对，只是同步精度与默认约定的取舍。

---

### 4.3 bind_kernel 与 PYBIND11_MODULE：成员指针顺序 = Python 参数顺序

#### 4.3.1 概念说明

前两节都在讲 Python 侧。现在翻到 C++ 侧，回答一个根本问题：**`from mk_llama import mk_llama` 拿到的那个函数，到底是怎么来的？它的参数顺序由谁决定？**

答案在 [llama.cu:26-53](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cu#L26-L53) 这一个 `PYBIND11_MODULE` 块里。它做两件事：

1. 用 `PYBIND11_MODULE(mk_llama, m)` 声明「我要造一个名叫 `mk_llama` 的 Python 模块」，`m` 是用来往里塞东西的句柄。
2. 调 `kittens::py::bind_kernel<内核类型>(m, "mk_llama", 一串成员指针...)`，把一个具体的 megakernel 模板实例**注册成模块里名为 `"mk_llama"` 的可调用对象**。

而那「一串成员指针」就是关键：它们**按出现顺序**定义了 Python 侧调用 `mk_llama(a0, a1, a2, ...)` 时，第 i 个参数会被绑到 `globals` 的哪个成员。这个顺序必须和 [latency/mk.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/mk.py) 里 `mk_func(...)` 的传参顺序**完全一致**，否则 Python 传的张量就会被塞进错误的 globals 槽位，内核读到张冠李戴的数据。

> 直觉：`bind_kernel` 像是在 Python 函数和 C++ 结构体之间拉了一根「位置对位置」的排线。你给出的成员指针列表，就是这根排线的接线表。

#### 4.3.2 核心流程：成员指针变元表契约

从两个调用点可以归纳出 `bind_kernel` 的对外契约（实现位于未检出的 ThunderKittens 子模块 `pyutils/pyutils.cuh`）：

```cpp
kittens::py::bind_kernel<
    /* 内核类型：mk<config, globals, op1, op2, ...> */
>(
    m,                       // pybind11 模块句柄
    "mk_llama",              // 在 Python 里暴露的名字
    &globals::field_0,       // ← Python 的第 0 个位置参数绑到这里
    &globals::field_1,       // ← Python 的第 1 个位置参数绑到这里
    &globals::field_2,       // ← …依此类推…
    ...
);
```

要点：

1. **模板参数 = 内核类型**。它是 `mk<config, globals, op1, ..., opN>`——`config` 是线程/共享内存配置，`globals` 是字段所在的结构体类型，后面跟一串该内核支持的 op 类型（如 `attention_partial_op`、`o_proj_op` 等）。这些 op 就是 [u9-l3] 讲过的那些 op，控制器会按指令的 opcode 派发到对应 op。
2. **变参成员指针 = Python 位置参数表**。成员指针的个数 = Python 侧位置参数的个数（latency 是 27 个）；成员指针的**顺序** = Python 侧传参的**顺序**。
3. **额外可选 `stream=`**。latency 的 `mk_func(..., stream=torch.cuda.current_stream())` 能传 `stream=`，说明 `bind_kernel` 在固定位置参数之外，还额外接受一个可选的 stream 关键字参数（用于指定 launch 所在 CUDA stream）。这超出成员指针列表，由绑定层单独处理。

把 [llama.cu:32-52](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cu#L32-L52) 的成员指针顺序，和 [latency/mk.py:15-48](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/mk.py#L15-L48) 的传参顺序对齐（Python 名 ↔ C++ 成员名）：

| # | Python 传参（latency/mk.py） | C++ 成员指针（llama.cu） | globals_t 字段 |
| --- | --- | --- | --- |
| 0 | `globs.barriers` | `&...::Bar` | `barriers Bar`（[cuh:108](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L108)） |
| 1 | `globs.instructions` | `&...::instructions` | `instruction_layout instructions`（[cuh:109](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L109)） |
| 2 | `globs.timings` | `&...::timings` | `timing_layout timings`（[cuh:110](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L110)） |
| 3 | `globs.qkv_proj_weights` | `&...::qkv_weights` | `weights_t qkv_weights`（[cuh:113](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L113)） |
| 4 | `globs.attn_ln_weights` | `&...::attn_norm_weights` | `norm_weights_t attn_norm_weights`（[cuh:114](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L114)） |
| 5 | `globs.o_proj_weights` | `&...::o_weights` | `weights_t o_weights`（[cuh:115](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L115)） |
| 6 | `globs.mlp_ln_weights` | `&...::mlp_norm_weights` | `norm_weights_t mlp_norm_weights`（[cuh:116](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L116)） |
| 7 | `globs.up_proj_weights` | `&...::up_weights` | `weights_t up_weights`（[cuh:117](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L117)） |
| 8 | `globs.gate_proj_weights` | `&...::gate_weights` | `weights_t gate_weights`（[cuh:118](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L118)） |
| 9 | `globs.down_proj_weights` | `&...::down_weights` | `weights_big_indim_t down_weights`（[cuh:119](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L119)） |
| 10 | `globs.lm_head_norm_weights.data` | `&...::lm_head_norm_weights` | `norm_weights_t lm_head_norm_weights`（[cuh:120](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L120)） |
| 11 | `globs.lm_head_weights.data` | `&...::lm_head_weights` | `weights_t lm_head_weights`（[cuh:121](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L121)） |
| 12 | `fourD_k_cache` | `&...::k_cache` | `kv_cache_t k_cache`（[cuh:123](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L123)） |
| 13 | `fourD_v_cache` | `&...::v_cache` | `kv_cache_t v_cache`（[cuh:124](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L124)） |
| 14 | `globs.rope_cos` | `&...::rope_cos` | `rope_table_t rope_cos`（[cuh:127](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L127)） |
| 15 | `globs.rope_sin` | `&...::rope_sin` | `rope_table_t rope_sin`（[cuh:128](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L128)） |
| 16 | `globs.hidden_states` | `&...::hidden_states` | `activations_t hidden_states`（[cuh:131](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L131)） |
| 17 | `globs.post_ln_rope_q` | `&...::q_post_rope` | `activations_t q_post_rope`（[cuh:132](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L132)） |
| 18 | `globs.attn_out` | `&...::attn_out` | `activations_t attn_out`（[cuh:133](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L133)） |
| 19 | `globs.attn_lse_intermediates` | `&...::attn_lse_intermediates` | `attn_lse_intermediates_t`（[cuh:134](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L134)） |
| 20 | `globs.attn_out_intermediates` | `&...::attn_out_intermediates` | `attn_out_intermediates_t`（[cuh:135](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L135)） |
| 21 | `globs.silu_out` | `&...::silu_out` | `activations_big_indim_t silu_out`（[cuh:136](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L136)） |
| 22 | `globs.logits` | `&...::logits` | `logits_t logits`（[cuh:137](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L137)） |
| 23 | `globs.pos_id` | `&...::pos_id` | `unsigned int pos_id`（[cuh:139](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L139)） |
| 24 | `globs.attn_scale` | `&...::attn_scale` | `float attn_scale`（[cuh:140](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L140)） |
| 25 | `globs.rms_norm_eps` | `&...::rms_norm_eps` | `float rms_norm_eps`（[cuh:141](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L141)） |
| 26 | `globs.skip_attn_reduction` | `&...::skip_attn_reduction` | `bool skip_attn_reduction`（[cuh:142](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L142)） |
| — | `stream=...`（关键字） | （绑定层单独处理，非成员指针） | — |

这就是本讲第二个核心实践任务要你建立的一一对应。注意三处「Python 名 ≠ C++ 名」但语义相同：`barriers↔Bar`、`attn_ln_weights↔attn_norm_weights`、`post_ln_rope_q↔q_post_rope`——位置对齐才是本质，名字只是风格差异。

#### 4.3.3 源码精读

**(a) op 别名与内核模板实例**（[llama.cu:15-24](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cu#L15-L24)）——先用 `using` 把每个 op 模板实例化好（`rms_qkv_rope_append_op`、`attention_partial_op`、`o_proj_op` 等，都是 `<default_config, llama_1b_globals>`）。这些 op 正是 [u9-l3] 全套讲完的 7 个 op。

**(b) `PYBIND11_MODULE` 主体**（[llama.cu:26-53](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cu#L26-L53)）：

```cpp
PYBIND11_MODULE(mk_llama, m) {            // 模块名 = mk_llama（Python 里 import mk_llama）
    m.doc() = "";
    kittens::py::bind_kernel<
        mk<default_config, llama_1b_globals,    // ← 内核类型：config + globals
            attention_partial_op, attention_reduction_op,
            rms_qkv_rope_append_op, downproj_op,
            o_proj_op, rms_upgate_silu_op, rms_lm_head_op>>(  // ← 该内核支持的 7 个 op
        m, "mk_llama",                          // ← 模块句柄 + Python 里暴露的名字
        &llama_1b_globals::Bar, &llama_1b_globals::instructions,
        &llama_1b_globals::timings,             // ← vm 三件套（位置 0/1/2）
        &llama_1b_globals::qkv_weights, ...      // ← 权重（位置 3+）
        ... &llama_1b_globals::skip_attn_reduction);  // ← 最后一个位置参数（位置 26）
}
```

关键观察：

- **模块名 = `mk_llama` = Python 可调用对象名 = `get_mk_func` 里 `from mk_llama import mk_llama` 的两个 `mk_llama`**（[mk.py:6-7](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/mk.py#L6-L7)）：第一个是模块文件名（编译产物 `mk_llama.so`），第二个是 `bind_kernel` 第二参数 `"mk_llama"` 注册的函数名。两者同名是约定，不是强制。
- **成员指针的分组与 `globals_t` 声明顺序一致**：vm stuff（Bar/instructions/timings）→ 权重 → kv cache → rope → 激活 → 标量。这和 [llama.cuh:107-142](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L107-L142) 的字段声明顺序、以及 `mk_func(...)` 的传参顺序三者对齐。这种「三处同序」是可维护性的关键——改一处必须同步改另两处。

**(c) 对照最简模板**（项目脚手架 [util/mk_init/sources/src/{{PROJECT_NAME_LOWER}}.cu:61-66](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/sources/src/%7B%7BPROJECT_NAME_LOWER%7D%7D.cu#L61-L66)）——「造你自己的 megakernel」模板里 `globals` 只有 `instructions` + `timings` 两个字段，于是 `bind_kernel` 也只传两个成员指针：

```cpp
kittens::py::bind_kernel<megakernel::mk<config, globals, TestOp>>(
    m, "example_megakernel", &globals::instructions, &globals::timings);
```

这说明成员指针列表是**完全由 globals 结构决定**的：globals 有几个需要从 Python 喂的字段，就传几个成员指针；它们的顺序就是 Python 调用的参数顺序。从 2 个字段（模板）到 27 个字段（llama），机制完全一样，只是接线表更长。

#### 4.3.4 代码实践

**目标**：验证「`llama.cu` 的 `bind_kernel` 成员指针顺序」与「`latency/mk.py` 的 `mk_func` 传参顺序」一一对应，并能解释每个成员指针指向的 globals 字段。

**操作步骤（源码阅读 + 对齐型）**：

1. 打开 [llama.cu:32-52](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cu#L32-L52)，从 `&llama_1b_globals::Bar` 开始，给每个成员指针编号 0–26。
2. 打开 [latency/mk.py:15-48](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/mk.py#L15-L48)，从 `globs.barriers` 开始，给每个位置参数编号 0–26。
3. 逐行核对编号相同的两项是否语义一致（允许名字不同，如 #0 `Bar`↔`barriers`、#17 `q_post_rope`↔`post_ln_rope_q`）。
4. 对任意一个成员指针，跳到 [llama.cuh:107-142](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L107-L142) 找到它指向的字段，确认类型（张量 / 标量）与 Python 侧传的 `globs.xxx` 类型匹配（张量字段对应 `Tensor`、`pos_id` 对应 `int`、`skip_attn_reduction` 对应 `bool`）。

**需要观察的现象**：27 个成员指针与 27 个位置参数严格按位置对齐；`stream=` 不在成员指针列表里（它是绑定层额外的可选参数）。

**预期结果**：你能指着 `bind_kernel` 里第 N 个 `&llama_1b_globals::xxx`，说出「Python 侧第 N 个参数 `mk_func(globs.yyy)` 会被绑到这里，对应 globals_t 的 `xxx` 字段（类型 …）」。

> 本地是否运行：纯源码阅读，无需 GPU。若想验证绑定真的生效，可在 [latency/mk.py:14](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/mk.py#L14) 的 `mk_func(...)` 前**临时**故意把两个参数对调（仅本地调试，勿提交），重跑 `mode=mk`：内核会读到错位的张量，输出立即变成噪声——这反向证明了顺序绑定的严格性。待本地验证。

#### 4.3.5 小练习与答案

**Q1**：如果有人在 `globals_t` 里新增了一个字段 `my_buffer`，并在内核里用了它，但忘了在 `bind_kernel` 里加对应的成员指针、也忘了在 `mk_func(...)` 里传参，会发生什么？

**答**：分两种情况。若内核只是「可能读」但当前没真读，可能暂不报错（潜伏 bug）。一旦内核真去读 `my_buffer`，它读到的是未初始化内容（成员指针没接线，Python 没喂值），结果错误甚至越界。更隐蔽的是：若只在 `bind_kernel` 加了成员指针但 `mk_func` 漏传，Python 调用会因**位置错位**把后续所有参数塞进错误的槽位——这正是「三处同序」必须严格遵守的原因。

**Q2**：`bind_kernel` 第二参数 `"mk_llama"` 和 `PYBIND11_MODULE(mk_llama, m)` 的 `mk_llama` 是同一个东西吗？

**答**：不是。`PYBIND11_MODULE(mk_llama, m)` 的 `mk_llama` 是**模块名**——编译出的 `.so` 叫 `mk_llama`，Python `import mk_llama` 拿到的是这个模块对象。`bind_kernel` 的第二参数 `"mk_llama"` 是在**模块内部**注册的一个**可调用对象的名字**——所以最终是 `mk_llama.mk_llama`（模块.函数）。代码里 `from mk_llama import mk_llama`（[mk.py:7](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/mk.py#L7)）正是「从模块 mk_llama 导入函数 mk_llama」。两者同名是项目约定，方便记忆，并非 pybind11 强制。

**Q3**：为什么成员指针列表里没有 `grid`/`block`/`dynamic_shared_memory`（[llama.cuh:144-146](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L144-L146)）这些启动配置方法？

**答**：`grid()`/`block()`/`dynamic_shared_memory()` 是 `globals_t` 的**成员函数**（返回 `dim3`/`int`），不是数据字段，其值由 `config` 与 `sm_count` 等编译期常量决定（如 `grid() = dim3(sm_count)`）。它们属于「内核如何 launch」的元信息，由 `bind_kernel` 在 C++ 侧直接从 globals 类型读出，不需要、也不应该从 Python 传入。Python 只负责喂数据字段（权重/激活/标量），launch 配置是内核自己的事。

---

## 5. 综合实践：把「Python 传参 → 成员指针 → globals 字段」整条链走通

把本讲三个模块串起来，做一次端到端的「接线表」核验，覆盖 dispatch 选型、参数差异、绑定对应三件事。

**任务背景**：假设你要给低延迟 demo **新增一个调试标量 `dump_timings`（bool）**，让内核可选择性地把 timings 打印出来。请把这件事在「Python dataclass → mk.py 传参 → llama.cu 绑定 → globals_t 字段」四处都改对，并说明顺序约束。

**操作步骤**：

1. **Python dataclass**：在 [latency/instructions.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/instructions.py) 的 `Globals` 里加 `dump_timings: bool`（参考已有的 `skip_attn_reduction`，[L19](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/instructions.py#L19)）。
2. **mk.py 传参**：在 [latency/mk.py:47](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/mk.py#L47) 的 `mk_func(...)` 末尾、`stream=` **之前**，加 `globs.dump_timings`（注意：它必须作为位置参数，紧跟在 `skip_attn_reduction` 后；`stream=` 永远是最后的关键字）。
3. **globals_t 字段**：在 [llama.cuh:139-142](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L139-L142) 的标量区，`skip_attn_reduction` 之后加 `bool dump_timings;`。
4. **bind_kernel 绑定**：在 [llama.cu:52](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cu#L52) 的成员指针列表末尾，`&llama_1b_globals::skip_attn_reduction` 之后加 `&llama_1b_globals::dump_timings`。

**需要观察的现象 / 预期结果**：

- 四处的**新增项位置必须完全一致**：dataclass 字段顺序、`mk_func` 传参顺序、`globals_t` 字段顺序、`bind_kernel` 成员指针顺序，四张表的「最后一项」都得是 `dump_timings`（在 `skip_attn_reduction` 之后、任何 `stream=` 之前）。
- 若只在三处改、漏一处，最典型的故障是「位置错位」：比如忘了在 `bind_kernel` 加成员指针但 `mk_func` 多传了一个，那么 `dump_timings` 这个 bool 会被绑到 `skip_attn_reduction` 槽、而真正的 `skip_attn_reduction` 反而错位——内核读到张冠李戴的标量。
- **dispatch.py 一行都不用改**：因为 `make_mk_interpreter` 只负责选 `LatencyMK_Interpreter` 类，不关心它内部传哪些字段。这验证了 4.1 的「工厂收口」设计——新增字段是「族内部」的事，不扩散到调度层。

**验收**：如果你能不看答案说出「这四处各加一行、且顺序必须对齐、dispatch 不动」，说明你已经把本讲「工厂映射 + 参数差异 + 绑定对应」三件事在源码层面打通了。

> 本地是否运行：本任务是「设计型源码阅读」，不需要真去改源码（本讲禁止改源码）。若要真验证，需在 Hopper/Blackwell 环境重编 `mk_llama` 后跑 `mode=mk`，观察 timings 是否按预期打印——**待本地验证**。

---

## 6. 本讲小结

- [dispatch.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/dispatch.py) 用**三张字典**（`BUILDER_MAP`/`MK_INTERPRETER_MAP`/`INSTRUCTION_TO_SOLVER_MAP`）+ **三个工厂**（`make_schedule_builder`/`make_mk_interpreter`/`make_pyvm_interpreter`），按 `setting`（latency/throughput）选一整套调度器/解释器/solver；`generate.py` 只认工厂函数。命名陷阱：dispatch 形参叫 `mode`，实际收到的是 `config.setting`。
- `mode`（torch/pyvm/mk，选执行器）与 `setting`（latency/throughput，选实现族）是**正交两个维度**；throughput 在 [generate.py:59-75](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py#L59-L75) 还会改 `mk_dir`、`batch_size=1024`、`interleave_rope=False`，且 `BATCH_SIZE` 编译期写死。
- `LatencyMK_Interpreter` 与 `ThroughputMK_Interpreter` 共享「一次 `mk_func(globs...)` 喂全部状态」的骨架；差异在：throughput 多传 `batch_size` 与一组 `rms_*_intermediates`（批化 RMS 中间量、无 partial/reduction），latency 多传 `attn_lse/attn_out_intermediates`（跨 SM partial+reduction）+ `skip_attn_reduction` 调试开关 + `stream=`。
- `PYBIND11_MODULE(mk_llama, m)` 造出名为 `mk_llama` 的 Python 模块；`kittens::py::bind_kernel<mk<config,globals,op...>>(m, "mk_llama", 成员指针...)` 把内核注册成模块里的可调用对象，**成员指针顺序 = Python 位置参数顺序**。
- 这套顺序与 [latency/mk.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/mk.py) 的传参顺序、[llama.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh) 的 `globals_t` 字段声明顺序**三处同序**；`stream=` 是绑定层额外的可选关键字参数，不在成员指针列表里。新增字段必须四处同步、且 dispatch 不用改。

---

## 7. 下一步学习建议

- **读吞吐内核本体**：throughput 的 megakernel 源码由脚本生成到 `tests/batch-vm/llama_official`（见 [generate.py:59-66](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py#L59-L66)）。生成后对照本讲 4.2 的参数表，验证它的 `bind_kernel` 成员指针顺序与 `ThroughputMK_Interpreter` 传参顺序一一对齐（注意 `batch_size`、`rms_*_intermediates` 的位置）。
- **深入 pybind11 与 kittens 绑定层**：检出 ThunderKittens 子模块，读 `pyutils/pyutils.cuh` 里 `bind_kernel` 的真实实现，确认「成员指针 → Python 参数」是如何用 pybind11 的 `def` + 可变参数模板展开完成的。这能把本讲「从调用点归纳的契约」升级成「从源码确认的实现」。
- **对比两套指令集的 solver**：读 [demos/throughput/python_vm.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/throughput/python_vm.py) 与 latency 版的 `INSTRUCTION_TO_SOLVER`，看同一族里 10 条 throughput opcode 各自对应哪个 PyTorch solver，呼应 [u2-l1] 的 pyvm 桥梁作用。
- **造你自己的 megakernel**：跑项目脚手架 `util/mk_init`（模板见 [{{PROJECT_NAME_LOWER}}.cu:61-66](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/sources/src/%7B%7BPROJECT_NAME_LOWER%7D%7D.cu#L61-L66)），从只有 `instructions`+`timings` 两个字段的最简 `bind_kernel` 开始，亲手体会「globals 字段决定成员指针列表」的机制。
