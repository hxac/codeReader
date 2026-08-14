# 框架使能与 DFX 调测

## 1. 本讲目标

本讲是 Autofuse「能跑起来」之后的下一公里：**怎么让一个真实网络用上 Autofuse，怎么确认它真的融合了，融合出问题时又怎么调测**。读完本讲你应该能够：

- 用一行 `torch.compile(options={"npu_backend": "ascendc"})` 在 PyTorch 中使能 Autofuse，并能解释这行代码触发了什么。
- 在编译产物目录里找到 `autofused_` 前缀的融合算子产物，读懂它代表「融合成功」这一信号。
- 当某个算子没有融合（fallback）时，知道去哪里找原因，并列举常见的 fallback 触发条件。
- 区分两套调测开关：torch 原生的 `TORCH_COMPILE_DEBUG` 与 Autofuse 自己的 `AUTOFUSE_DFX_FLAGS`，知道它们各自落盘什么、由哪段源码消费。

本讲承接 u3-l2（Autofuse 六大模块总览与数据流）。在那一讲里我们已经知道 Autofuse 是「输入计算子图、输出 C++ kernel」的编译器；本讲就回答「框架怎么把子图喂给它、它吐出的产物落在哪、怎么看」。

## 2. 前置知识

- **torch.compile 与后端（backend）**：PyTorch 2.x 的编译入口 `torch.compile(model)` 会把模型的前向图交给一个「后端」去编译优化。默认后端是 Inductor；Autofuse 通过一个名为 `ascendc` 的自定义后端接入（该后端实现在 `torch_npu` 中，不在本仓库内）。本仓库的 Autofuse 编译器是这个后端调用的「内核生成器」。
- **Inductor 与 lowering**：Inductor 会把高层 ATen 算子「降级（lowering）」成底层算子。**没有被 Inductor lowering 的算子，Autofuse 接触不到，只能以单算子形式存在**——这是后面 fallback 的主要来源。
- **DFX（Design for eXcellence / 调测）**：业界泛指为可调试、可观测、可诊断而预留的能力。在 Autofuse 里，DFX 主要指「把编译中间产物和内部融合图落盘」的一组开关。
- **两层中间产物**：① torch 层的产物（`torch_compile_debug/` 目录）；② Autofuse 自身的产物（落盘的 host/device 源码、融合图 pbtxt）。理解「这两层由不同开关控制」是本讲的核心难点。

> 关键直觉：Autofuse 本身只是个「编译器函数」，它没有自己的训练循环。它之所以能在网络里生效，全靠 `torch.compile` + `ascendc` 后端把网络子图喂给它。所以「使能」本质是「接线」，而「调测」本质是「让接线过程把中间产物留下来」。

## 3. 本讲源码地图

本讲涉及的文件按职责分为三组：

| 文件 | 作用 |
|------|------|
| [autofuse/examples/pytorch/af_pointwise/af_add_ge.py](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/examples/pytorch/af_pointwise/af_add_ge.py) | 最小使能示例：`add + ge` 两个 pointwise 算子融合，并带 profiling |
| [autofuse/examples/pytorch/README.md](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/examples/pytorch/README.md) | 用例执行说明，定义「`autofused_` 开头 kernel = 融合成功」的判据 |
| [autofuse/README.md](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/README.md) | 权威调测文档：四个环境变量（`TORCH_COMPILE_DEBUG` 等）与 `autofused_`/fallback 说明 |
| [autofuse/compiler/python/compile_adapter.py](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/compiler/python/compile_adapter.py) | host/device 编译编排；解析 `AUTOFUSE_DFX_FLAGS`，决定是否保留中间源码 |
| [autofuse/compiler/python/ascendc_compile.py](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/compiler/python/ascendc_compile.py) | 底层编译驱动；`codegen_compile_debug` 开启每 pass 计时与编译时间线 |
| [autofuse/ascir/meta/ascir_utils.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascir/meta/ascir_utils.cpp) | C++ 侧 DFX 实现：解析 `debug_dir`、按 pid/图名分目录落盘融合图 pbtxt |
| [autofuse/common/autofuse_config/auto_fuse_config_parser.cc](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/common/autofuse_config/auto_fuse_config_parser.cc) | C++ 侧环境变量解析器：白名单机制过滤 `AUTOFUSE_FLAGS` / `AUTOFUSE_DFX_FLAGS` |

> 提示：使能与 `autofused_` 产物的「生成」动作发生在 `torch_npu` 的 AscendC 后端里（不在本仓库），所以本讲在讲这两块时会**区分「本仓库做了什么」和「后端做了什么」**，绝不把别处的逻辑说成这里的代码。而 DFX 环境变量的**消费端**大量就在本仓库内，是本讲源码精读的重点。

## 4. 核心概念与源码讲解

### 4.1 框架使能方式：一行 torch.compile

#### 4.1.1 概念说明

「使能 Autofuse」并不是去 import 一个 Autofuse 模块，而是在 `torch.compile` 里指定 AscendC 后端。框架在编译模型时，会把能够 lowering 的相邻 Vector 算子打包成子图，经 AscendC 后端交给本仓库的 Autofuse 编译器（Optimizer → ATT → Codegen，见 u3-l2），生成一个融合 kernel，再把该 kernel 装回原图替换掉原来的若干算子。

关键点：**用户代码里看不到 Autofuse 的任何符号**。Autofuse 对用户是「隐身」的，只通过 `options={"npu_backend": "ascendc"}` 这个开关被激活。README 在「复杂网络使能」一节明确说：无需单独导入 `inductor_npu_ext`，只需在 `torch.compile` 中指定 AscendC 后端即可。

#### 4.1.2 核心流程

使能到产物的链路可以拆成 5 步：

1. **写模型**：用普通 `nn.Module` 写前向，里面是若干相邻的 elementwise / reduce 算子。
2. **包 compile**：`model = torch.compile(model, options={"npu_backend": "ascendc"})`。
3. **框架切图**：torch/Inductor 抓取前向图，对可 lowering 的相邻 Vector 算子识别出融合候选子图。
4. **Autofuse 编译**：AscendC 后端把子图喂给本仓库 Autofuse，产出 host tiling 代码 + device kernel 源码，再编译成 `.o`/`.run`。
5. **回填执行**：融合 kernel 替换原图中的算子组，运行时只下发一个 kernel。

`fullgraph=True` 表示「整图编译，不允许图断裂」；`dynamic=False` 表示关闭动态 shape（用静态 shape 编译，ATT 的 tiling 求解更稳定）。这两个参数在示例里都会出现。

#### 4.1.3 源码精读

最简使能写法来自示例 `af_add_ge.py`。模型只有一行前向，把 `add` 与 `ge` 串起来：

[autofuse/examples/pytorch/af_pointwise/af_add_ge.py:23-29](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/examples/pytorch/af_pointwise/af_add_ge.py#L23-L29) — 定义 `MyModel`，前向里 `result = torch.ge(torch.add(x, y), z)`，这正是 Autofuse 要融合的「加法 + 比较」pointwise 链。

使能 Autofuse 的唯一关键代码就是这一段：

[autofuse/examples/pytorch/af_pointwise/af_add_ge.py:33-38](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/examples/pytorch/af_pointwise/af_add_ge.py#L33-L38) — `torch.compile(model, dynamic=False, fullgraph=True, options={"npu_backend": "ascendc"})`。`options` 里的 `npu_backend` 键就是激活 AscendC（Autofuse）后端的开关。

示例同时演示了如何用 `torch_npu.profiler` 采集性能数据，便于第 4.3 节对照性能收益：

[autofuse/examples/pytorch/af_pointwise/af_add_ge.py:61-72](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/examples/pytorch/af_pointwise/af_add_ge.py#L61-L72) — 在 profiler 上下文里循环跑 100 次，把结果输出到 `./profiling` 目录。

README 的「复杂网络使能」给出了与示例完全一致的写法，并强调无需额外 import：

[autofuse/README.md:151-162](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/README.md#L151-L162) — 说明在真实网络中使能 Autofuse 只需同样的 `torch.compile(..., options={"npu_backend": "ascendc"})`。

> 说明：`ascendc` 这个后端名字符串的「解析与分发」逻辑在 `torch_npu` 仓库中（即「Inductor NPU 扩展」），不在本仓库。本仓库提供的是被它调用的 Autofuse 编译器本身。因此本节不引用该分发逻辑的源码——它不在当前代码树里。

#### 4.1.4 代码实践

- **实践目标**：在不实际跑板的情况下，确认自己能写出「最小使能片段」并理解每个参数的含义。
- **操作步骤**：
  1. 打开 `autofuse/examples/pytorch/af_pointwise/af_add_ge.py`。
  2. 在纸上写下三问：`fullgraph=True` 关闭了什么？`dynamic=False` 关闭了什么？`npu_backend` 取值 `ascendc` 激活了谁？
  3. 把示例里的模型前向改成 `result = torch.add(torch.mul(x, y), z)`（mul + add），判断这段是否能被 Autofuse 融合（均为 pointwise，预期可融合）。
- **需要观察的现象**：无需运行；重点是把「使能 = 一个 options 键」这一事实记牢。
- **预期结果**：能复述「Autofuse 对用户隐身，只靠 `npu_backend=ascendc` 激活；AscendC 后端在 torch_npu 中，不在本仓库」。
- **待本地验证**：若在昇腾环境，按 u1-l4 准备 CANN/torch_npu，`cd autofuse/examples/pytorch/af_pointwise && python af_add_ge.py`，观察终端是否顺利跑完 100 step。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `fullgraph=True` 改成不写（默认 False），对 Autofuse 可能有什么影响？
**答案**：默认允许「图断裂」，框架可能在某些算子处切断编译图，导致原本可融合的相邻算子被拆到不同子图，减少融合机会。

**练习 2**：用户代码里没有任何 `import autofuse`，为什么 Autofuse 还能生效？
**答案**：因为使能入口是 `torch.compile` 的 `options={"npu_backend":"ascendc"}`，由 torch_npu 的 AscendC 后端在编译时自动调用本仓库的 Autofuse 编译器，用户无需显式导入。

---

### 4.2 autofused_ 产物与 fallback 原因

#### 4.2.1 概念说明

使能之后，怎么判断「真的融合了」？有两类观察点，分别来自两个不同位置：

1. **编译期产物（torch 层）**：开启 `TORCH_COMPILE_DEBUG` 后，当前目录会生成 `torch_compile_debug/` 子目录。其中**以 `autofused_` 为前缀的目录**就是 AscendC 后端为「每一个融合算子」生成的白盒结构产物（包含融合范围与代码生成结果）。其余目录是 PyTorch Inductor 的原生产物。
2. **运行期产物（profiling 层）**：profiling 产出的 `op_summary_*.csv` 里，如果出现**以 `autofused_` 开头的 kernel 名**，表示相关算子已成功融合成一个融合算子。

判据很清晰：**有 `autofused_` = 融合成功；没有 = 没产生融合算子**。当没有时，就需要分析 fallback 原因。

> 重要边界：`autofused_` 前缀的「目录」和「kernel 名」都是由 `torch_npu` 的 AscendC 后端在编译/运行期生成的，**本仓库源码中并不产生这两个产物**（在本仓库内全文检索 `autofused_`，命中的是文档与测试里的固定字符串，例如测试辅助代码里写死的 `OP_NAME`）。因此本节以 README 这份权威文档为依据讲解，并指出本仓库内能看到的「同族」证据。

#### 4.2.2 核心流程

判断融合成败的决策树：

```
开启 TORCH_COMPILE_DEBUG
        │
        ▼
跑模型 ──► 看 torch_compile_debug/
        │
        ├── 有 autofused_* 目录 ──► 该融合算子编译成功（白盒产物可查）
        │
        └── 无 autofused_* 目录 ──► 终端搜 "Fallback aten.xxxx $reason"
                                        │
                                        ▼
                                  按 reason 分析 fallback 原因
```

常见 fallback 原因（依据 README 与 Inductor 工作原理）：

- **算子未被 Inductor lowering**：README 明确「对于在 Inductor 层未被 lowering 的算子，最后仍然以单算子形式存在」。这类算子根本进不到 AscendC 后端，自然无法融合。
- **算子未在 ASCIR 注册**：即使 lowering 进来了，若该算子不在 Autofuse 的 ASCIR 算子注册表里（见 u5-l1），codegen 无法处理，会回退。
- **融合约束不满足**：例如算子之间夹杂了不可融合的节点、shape 不连续、触发图断裂等。

README 给出的性能度量口径（注意是「耗时下降比」不是「kernel 数减少比」）：

\[
\text{融合提升比} = \frac{\text{融合前所有算子总耗时} - \text{融合后所有算子总耗时}}{\text{融合前所有算子总耗时}}
\]

更细粒度地，可以对比融合算子相对单算子的 `aiv_mte2_time`（输入搬运耗时）与 `aiv_mte3_time`（输出搬运耗时）——这正是 u3-l1 讲过的 Memory Bound 量化指标。

#### 4.2.3 源码精读

`autofused_` 的判据来自 README「结果分析」一节，这是本仓库内对产物含义最权威的定义：

[autofuse/README.md:131-132](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/README.md#L131-L132) — 明确：`torch_compile_debug` 下以 `autofused_` 为前缀的目录对应一个融合算子的白盒结构；若未生成，说明没有融合算子，应依据终端 `Fallback aten.xxxx $reason` 分析未融合原因。

profiling 判据来自用例 README：

[autofuse/examples/pytorch/README.md:88-100](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/examples/pytorch/README.md#L88-L100) — 指出在 `op_summary_*.csv` 中若存在以 `autofused_` 开头的 kernel，即表示融合成功。

性能度量口径与 mte2/mte3 指标：

[autofuse/README.md:143-147](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/README.md#L143-L147) — 定义融合提升比公式，并指明可观察 `aiv_mte2_time` / `aiv_mte3_time` 的改善。

本仓库内能看到的「同族」证据——codegen 生成 kernel 时其 `OP_NAME` 就遵循 `autofused_` 命名（这是测试/落盘 tiling 头里的固定样例名）：

[autofuse/codegen/codegen_tiling.cpp:634](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/codegen/codegen_tiling.cpp#L634) — 写死 `#define OP_NAME "asc0000_autofused_abs"`，体现了 Autofuse 生成 kernel 的 `autofused_<算子名>` 命名约定（此为测试辅助代码中的样例字符串）。

> 边界提醒：本仓库源码全文没有 `Fallback aten.xxxx $reason` 这条字符串（仅在 README 文档中出现）。这条 fallback 日志由 torch_npu 的 AscendC 后端在编译期打印，不在当前代码树。分析 fallback 时应去**终端日志**里找，而不是在本仓库里搜。

#### 4.2.4 代码实践

- **实践目标**：建立「`autofused_` 是融合成功的判据；找不到就去查 fallback」的排查肌肉记忆。
- **操作步骤**：
  1. 阅读 [autofuse/README.md:131-132](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/README.md#L131-L132) 与 [autofuse/examples/pytorch/README.md:88-100](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/examples/pytorch/README.md#L88-L100)。
  2. 写出两种「融合成功」的观测位置：编译期（`torch_compile_debug/autofused_*` 目录）与运行期（`op_summary_*.csv` 中 `autofused_` 开头 kernel）。
  3. 写出一条「导致 fallback 的可能原因」：例如「模型里某个算子未被 Inductor lowering，最后仍以单算子形式存在」。
- **需要观察的现象**：纯文档阅读型实践，无需运行。
- **预期结果**：能说出至少两条 fallback 原因（未被 lowering / 未在 ASCIR 注册 / 触发图断裂等），并知道 fallback 日志要去终端而非本仓库找。
- **待本地验证**：若在昇腾环境，跑 `af_add_ge.py` 并 `export TORCH_COMPILE_DEBUG=1`，到 `torch_compile_debug/` 下确认存在 `autofused_` 前缀目录。

#### 4.2.5 小练习与答案

**练习 1**：`torch_compile_debug/` 下既有 `autofused_*` 目录也有别的目录，二者分别由谁生成？
**答案**：`autofused_*` 前缀目录由 torch_npu 的 AscendC 后端（调用本仓库 Autofuse）生成，是融合算子白盒产物；其余目录是 PyTorch Inductor 的原生产物。

**练习 2**：终端出现 `Fallback aten.ge $reason: ...`，最该先排查什么？
**答案**：先确认该算子是否被 Inductor lowering、是否在 Autofuse 的 ASCIR 注册表中；若两者都满足，再排查融合约束（图断裂、shape、相邻不可融合节点等）。

**练习 3**：为什么融合提升比用「耗时」而非「kernel 个数」衡量？
**答案**：因为融合的根本收益是减少全局内存搬运、缓解 Memory Bound，体现在 `aiv_mte2/mte3` 耗时下降；kernel 个数减少只是手段，耗时下降才是目的（见 u3-l1）。

---

### 4.3 DFX / Profiling 环境变量

#### 4.3.1 概念说明

这是本讲源码最深的一节。Autofuse 提供了一套**自有的 DFX 环境变量**，与 torch 原生调试变量分工不同：

| 环境变量 | 来源 | 作用 | 落盘位置 |
|----------|------|------|----------|
| `TORCH_COMPILE_DEBUG=1` | torch 原生 | 开启详细调试日志、保存 torch 层编译中间产物 | 当前目录 `torch_compile_debug/` |
| `TORCHINDUCTOR_FORCE_DISABLE_CACHES=1` | torch 原生 | 禁用 Inductor 缓存，强制每次重编译 | — |
| `ASCEND_LAUNCH_BLOCKING=1` | torch_npu 原生 | kernel 同步下发，定位首个报错 kernel | — |
| `AUTOFUSE_DFX_FLAGS="..."` | **Autofuse 自有** | 落盘每个融合算子的内部融合图、控制编译性能诊断 | 由 `--debug_dir` 指定或当前目录 |

最关键的区分：**`TORCH_COMPILE_DEBUG` 保存 torch 层产物（含 `autofused_` 目录）；`AUTOFUSE_DFX_FLAGS` 保存 Autofuse 自身的产物（host/device 中间源码 + 融合图 pbtxt + 编译时间线）**。两者是不同层，常常配合使用。

`AUTOFUSE_DFX_FLAGS` 的格式是「分号分隔的 `--key=value` 串」，两个最常用键：

- `--codegen_compile_debug=true`：开启编译性能诊断（每 LLVM pass 耗时 + 编译时间线 JSON）+ **保留 Autofuse 中间源码不清理** + **开启融合图落盘**。
- `--debug_dir=/path/`：指定落盘根目录；不设则落到当前目录。

#### 4.3.2 核心流程

`AUTOFUSE_DFX_FLAGS` 在两个语言层各有一条消费链：

```
AUTOFUSE_DFX_FLAGS="--codegen_compile_debug=true;--debug_dir=/d"
        │
        ├──► Python 层（compile_adapter.py / ascendc_compile.py）
        │       parse_env_flags() ──► get_debug_flag()
        │       · 控制 auto_cleanup：debug 开则不删中间 host/device 源码
        │       · get_compile_diagnostic_flags()：加 -ftime-report=per-pass、-ftime-trace
        │       · 产出编译时间线 JSON（~/.cache/autofuse_compile_trace）
        │
        └──► C++ 层（ascir_utils.cpp / auto_fuse_config_parser.cc）
                ParseDfxFlags() ──► DumpConfig{enabled, debug_dir}
                · IsCodegenCompileEnabled() 控制 DumpGraph 是否真正落盘
                · 路径：<debug_dir>/autofuse_compile_debug/ascgen_dump_pid_<pid>/<融合图名>/*.pbtxt
                · AlwaysDumpGraph()：debug 未开但异常退出时强制 dump
```

落盘目录结构（来自源码，不是文档转述）：

```
<debug_dir 或 ./>/
└── autofuse_compile_debug/
    └── ascgen_dump_pid_<进程号>/
        └── <融合图名>/          # 每个融合算子一个子目录
            ├── *_BeforeUnfoldAscBackend.pbtxt   # 各优化阶段的融合图快照
            ├── *_AfterConcatInputUnificationPass.pbtxt
            └── ...
```

这些 `.pbtxt` 可以用 [netron.app](https://netron.app) 打开，直观查看 Autofuse 内部各阶段的融合图结构。

#### 4.3.3 源码精读

**Python 层：解析 DFX 并控制是否清理中间源码**

`compile_adapter.py` 用一个通用函数解析「分号分隔的 `--key=value`」环境变量：

[autofuse/compiler/python/compile_adapter.py:321-336](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/compiler/python/compile_adapter.py#L321-L336) — `parse_env_flags` 按 `;` 切分、按 `=` 取键值、`lstrip("-")` 去掉 `--`；`get_dfx_env_result` 固定读 `AUTOFUSE_DFX_FLAGS`。

`get_debug_flag` 取出 `codegen_compile_debug` 是否为 `true`：

[autofuse/compiler/python/compile_adapter.py:339-341](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/compiler/python/compile_adapter.py#L339-L341) — `get_debug_flag()` 读取 `codegen_compile_debug` 键，默认 `false`。

这个标志直接决定 Autofuse 编译用的临时目录是否被自动清理——**debug 开启时保留中间 host/device 源码**，方便白盒查看：

[autofuse/compiler/python/compile_adapter.py:404-411](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/compiler/python/compile_adapter.py#L404-L411) — `auto_cleanup = not args.output_path and not get_debug_flag()`；`auto_cleanup` 为真时用 `TemporaryDirectory` 用完即删，为假时保留 `temp_dir`。

底层编译驱动 `ascendc_compile.py` 在 debug 开启时追加编译器诊断参数，并打印编译时间线路径：

[autofuse/compiler/python/ascendc_compile.py:150-169](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/compiler/python/ascendc_compile.py#L150-L169) — `get_compile_diagnostic_flags`：当 `codegen_compile_debug=true` 时返回 `["-ftime-report=per-pass", "-ftime-trace=<json>"]`，并在终端打印 `[CompileTrace] <文件路径>`，时间线 JSON 默认存 `~/.cache/autofuse_compile_trace`。

缓存目录常量就在文件顶部：

[autofuse/compiler/python/ascendc_compile.py:48-53](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/compiler/python/ascendc_compile.py#L48-L53) — 定义 `PCH_CACHE_ROOT=~/.cache/autofuse_pch_cache` 与 `COMPILE_TRACE_ROOT=~/.cache/autofuse_compile_trace`，与 README 描述一致。

**C++ 层：解析 DFX 并按 pid/图名分目录落盘融合图**

`ascir_utils.cpp` 定义落盘配置结构体与解析逻辑：

[autofuse/ascir/meta/ascir_utils.cpp:164-168](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascir_utils.cpp#L164-L168) — `DumpConfig{ bool enabled; std::string debug_dir; }`，两个字段分别对应 `codegen_compile_debug` 与 `debug_dir` 两个键。

[autofuse/ascir/meta/ascir_utils.cpp:220-247](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascir_utils.cpp#L220-L247) — `ParseDfxFlags` 按 `;` 切分 `AUTOFUSE_DFX_FLAGS`，`IsCodegenCompileDebugEnabled` 置 `enabled`，`TryParseDebugDir` 取 `debug_dir`；`GetDumpConfig` 直接读环境变量。

落盘目录的构造逻辑：

[autofuse/ascir/meta/ascir_utils.cpp:325-366](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascir_utils.cpp#L325-L366) — `InitDumpDirectories` 构造 `<debug_dir>/autofuse_compile_debug/ascgen_dump_pid_<pid>/`；`GetDumpGraphPrefixAndCreateDir` 在其下按当前融合图名再建子目录（每个融合算子一个目录）。

`DumpGraph` 与 `AlwaysDumpGraph` 的门控互斥设计是本节的精髓：

[autofuse/ascir/meta/ascir_utils.cpp:795-811](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascir/meta/ascir_utils.cpp#L795-L811) — `DumpGraph`：debug 未开时不落盘，而是把图对象缓存进线程局部上下文（异常退出时维测）；`AlwaysDumpGraph`：debug 已开时不重复落盘，**debug 未开但异常时强制 dump**。二者互补，保证「正常时按需落盘、出错时总能拿到现场」。

`IsCodegenCompileEnabled` 就是读 `DumpConfig().enabled`：

[autofuse/ascir/meta/ascir_utils.cpp:1058-1060](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascir/meta/ascir_utils.cpp#L1058-L1060) — `IsCodegenCompileEnabled()` 返回 `GetDumpConfig().enabled`，是所有 C++ 侧 DFX 落盘的总开关。

**C++ 层：白名单解析器（安全过滤）**

`auto_fuse_config_parser.cc` 用白名单机制解析 `AUTOFUSE_FLAGS` 与 `AUTOFUSE_DFX_FLAGS`，只有登记过的键才会被采纳：

[autofuse/common/autofuse_config/auto_fuse_config_parser.cc:33-54](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/common/autofuse_config/auto_fuse_config_parser.cc#L33-L54) — `ParseFlags` 按 `;` 切分、`=` 取键值、去掉 `--`，**最后只保留在白名单 `white_list` 中的键**。

[autofuse/common/autofuse_config/auto_fuse_config_parser.cc:56-71](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/common/autofuse_config/auto_fuse_config_parser.cc#L56-L71) — `AutoFuseEnvConfigParser::Parse` 分别读 `MM_ENV_AUTOFUSE_FLAGS` 与 `MM_ENV_AUTOFUSE_DFX_FLAGS`，用各自的白名单（`flags_config_keys_` / `dfx_flags_config_keys_`）过滤。

> 设计要点：白名单机制意味着「随手加一个未知键不会生效」，DFX 键必须在白名单登记。`AUTOFUSE_DFX_FLAGS` 登记的键（如 `att_profiling`、`autofuse_enable_tiling_cache` 等）定义在 [autofuse/common/autofuse_config/auto_fuse_config.h:27-41](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/common/autofuse_config/auto_fuse_config.h#L27-L41)，其中 `codegen_compile_debug` / `debug_dir` 由 ascir_utils 单独解析（见上），其余调优键经此白名单进入 `AttStrategyConfig`。

README 对四个环境变量的权威说明：

[autofuse/README.md:111-129](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/README.md#L111-L129) — `AUTOFUSE_DFX_FLAGS` 用法：`--codegen_compile_debug=true` 开每 pass 计时与编译时间线（存 `~/.cache/autofuse_compile_trace`，终端打印 `[CompileTrace]`），Host 编译复用 PCH（缓存于 `~/.cache/autofuse_pch_cache`，失败回退普通编译）。

#### 4.3.4 代码实践

- **实践目标**：追踪 `AUTOFUSE_DFX_FLAGS="--codegen_compile_debug=true;--debug_dir=/tmp/afdump"` 从被读取到改变行为的全过程。
- **操作步骤**：
  1. 在 [compile_adapter.py:321-341](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/compiler/python/compile_adapter.py#L321-L341) 跟踪 `parse_env_flags → get_dfx_env_result → get_debug_flag`，确认它读到 `true`。
  2. 在 [compile_adapter.py:404](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/compiler/python/compile_adapter.py#L404) 确认 `auto_cleanup` 变为 `False`，于是 [compile_adapter.py:410-411](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/compiler/python/compile_adapter.py#L410-L411) 的临时目录被保留。
  3. 在 [ascir_utils.cpp:245-251](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascir/meta/ascir_utils.cpp#L245-L251) 跟踪 `GetDumpConfig` 解析出 `{enabled=true, debug_dir="/tmp/afdump"}`。
  4. 在 [ascir_utils.cpp:325-366](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/ascir/meta/ascir_utils.cpp#L325-L366) 推导落盘路径应为 `/tmp/afdump/autofuse_compile_debug/ascgen_dump_pid_<pid>/<融合图名>/`。
- **需要观察的现象**：纯源码阅读型实践。
- **预期结果**：能画出「环境变量 → Python 两处消费 + C++ 两处消费」的四点追踪图，并预测出落盘目录。
- **待本地验证**：在昇腾环境设置该环境变量后跑 `af_add_ge.py`，到 `/tmp/afdump/autofuse_compile_debug/` 下确认生成了 pid 目录与 `.pbtxt` 融合图，用 netron 打开观察融合范围。

#### 4.3.5 小练习与答案

**练习 1**：`TORCH_COMPILE_DEBUG` 和 `AUTOFUSE_DFX_FLAGS` 各自负责保存哪一层产物？
**答案**：`TORCH_COMPILE_DEBUG` 保存 torch 层编译产物（含 `autofused_` 前缀的融合算子白盒目录）；`AUTOFUSE_DFX_FLAGS` 保存 Autofuse 自身产物（保留 host/device 中间源码、落盘融合图 pbtxt、编译时间线 JSON）。

**练习 2**：为什么 `DumpGraph` 和 `AlwaysDumpGraph` 要做成互斥？
**答案**：`DumpGraph` 在 debug 开启时正常落盘、未开启时只缓存图对象不落盘；`AlwaysDumpGraph` 反过来——debug 已开时正常流程已落盘就不再重复，debug 未开但程序异常退出时强制 dump 现场。两者互补，既避免正常情况重复落盘，又保证异常时总能拿到融合图供维测。

**练习 3**：用户写 `AUTOFUSE_DFX_FLAGS="--my_unknown_key=1"`，会生效吗？为什么？
**答案**：不会。C++ 侧 `ParseFlags` 有白名单机制，只采纳登记过的键；未知键会被丢弃。要新增可调键，必须先在白名单（如 [auto_fuse_config.h:27-41](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/common/autofuse_config/auto_fuse_config.h#L27-L41)）登记。

---

## 5. 综合实践

**任务：完成一次「使能 → 确认融合 → 双层调测产物对照」的全链路排查。**

1. **使能**：参照 [af_add_ge.py:33-38](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/examples/pytorch/af_pointwise/af_add_ge.py#L33-L38) 写出最小使能片段（`torch.compile` + `npu_backend=ascendc`）。
2. **开启双层调测**：同时设置
   ```bash
   export TORCH_COMPILE_DEBUG=1
   export AUTOFUSE_DFX_FLAGS="--codegen_compile_debug=true;--debug_dir=/tmp/afdump"
   ```
3. **确认融合（编译期）**：到 `torch_compile_debug/` 下找到 `autofused_` 前缀目录，确认融合算子白盒产物存在；若不存在，记录终端的 `Fallback ... $reason`。
4. **确认融合（Autofuse 层）**：到 `/tmp/afdump/autofuse_compile_debug/ascgen_dump_pid_<pid>/` 下找到融合图 `.pbtxt`，用 netron 打开，对照 `BeforeUnfoldAscBackend` / `AfterConcatInputUnificationPass` 等快照理解 u3-l2 讲过的 optimize 阶段。
5. **对照性能**：参照 [README:143-147](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/README.md#L143-L147)，注释掉 `torch.compile` 块跑一次「未融合」对照，用融合提升比公式比较两种场景的总耗时与 `aiv_mte2_time`/`aiv_mte3_time`。
6. **产出**：一张表，列出「torch 层产物路径 / Autofuse 层产物路径 / 是否融合成功 / fallback 原因（若有）/ 性能提升比」。

> 若无昇腾环境，步骤 3-5 改为「源码阅读型」：依据本讲引用的源码行号，在纸上推导出两层产物**应当**出现在哪些路径、由哪段代码生成，并说明判断依据。明确标注「待本地验证」。

## 6. 本讲小结

- **使能即接线**：Autofuse 对用户隐身，仅靠 `torch.compile(options={"npu_backend":"ascendc"})` 激活；AscendC 后端在 torch_npu 中，本仓库提供被调用的编译器。
- **`autofused_` 是融合判据**：编译期看 `torch_compile_debug/autofused_*` 目录，运行期看 `op_summary.csv` 里 `autofused_` 开头 kernel；二者都由 torch_npu 后端生成，不在本仓库。
- **fallback 去终端找**：未融合时搜 `Fallback aten.xxxx $reason`，常见原因是算子未被 Inductor lowering 或未在 ASCIR 注册；该日志由 torch_npu 打印，本仓库搜不到。
- **两套调测变量分工**：`TORCH_COMPILE_DEBUG`（torch 层产物）与 `AUTOFUSE_DFX_FLAGS`（Autofuse 自身产物：中间源码、融合图 pbtxt、编译时间线）。
- **DFX 消费链可追溯**：`AUTOFUSE_DFX_FLAGS` 在 Python（`compile_adapter.py`/`ascendc_compile.py`）与 C++（`ascir_utils.cpp`/`auto_fuse_config_parser.cc`）两侧都有真实消费代码，且 C++ 侧有白名单安全过滤。
- **DumpGraph/AlwaysDumpGraph 互补**：正常按需落盘，异常时强制 dump，保证总能拿到融合图现场。

## 7. 下一步学习建议

- 本讲只用到 Autofuse 的「外部入口」，尚未进入内部数据流。下一讲进入 u4（graph_metadef 图元数据），从 `autofused_` 产物回溯到「融合图」的底层表示 `ComputeGraph`/`Node`/`Operator`，理解 netron 里看到的那些节点到底对应哪些 C++ 类。
- 若你对「为什么有的算子不能融合」更感兴趣，可跳读 u5-l1（ASCIR 算子注册框架），看 `autofuse/ascir/generator/ascir_builtin_ops_v1.cpp` 里的注册清单——它决定了一个算子能否进入融合体系。
- 想深入 `AUTOFUSE_DFX_FLAGS` 的其它调优键（`att_algorithm`、`att_profiling`、`autofuse_enable_tiling_cache` 等），可在 u7（ATT 自动 Tiling）讲义中看到它们如何影响 tiling 求解。
