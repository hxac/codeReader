# 调试工具：lower trace、pass 可视化与 T.print

## 1. 本讲目标

学完本讲后，你应该能够：

- 区分 tilelang 的两类调试工具——**编译期**的 IR 差分工具（lower trace / pass_diff）与**运行期**的设备端打印（`T.print`），并知道何时该用哪一个。
- 会用环境变量 `TL_LOWER_TRACE` 或编程式 API `lower_trace.enable()` 打开「逐 Pass IR 差分」，找到 `report.html` 与各阶段 `.tir` 文件，定位某个 Pass（例如 `LayoutInference`）改了什么。
- 理解 `TILELANG_PASS_DIFF` 钩子（`pass_diff_hook`）的**零开销设计**：关闭时不打任何 monkey-patch、不额外 import。
- 会用 `tilelang.tools.pass_visualizer` 把 CUDA lowering 流水线渲染成「逐 Pass 的 SBlock 结构树」HTML，直观看到 tile op 何时被展开成硬件指令。
- 会在 kernel 里用 `T.print` 打印一个循环变量或缓冲，并理解它生成的是设备端 `debug_print_*` 调用、只在真正运行 kernel 时才输出。

本讲对应最小模块：`tilelang.utils.pass_diff_hook`、`tilelang.tools.lower_trace`、`tilelang.tools.pass_visualizer`，并补充运行期的 `T.print`。

## 2. 前置知识

本讲默认你已经学过：

- **u4-l1 / u6-l1**：tilelang 的编译总流程与 Pass 系统。一句话复习——用户写的 `@T.prim_func` 被封装成 `IRModule`，经一条「Pass 流水线」（每个 Pass 都是 `IRModule → IRModule` 的纯函数）逐步变形，最后由 device codegen 生成 CUDA/HIP 源码。
- **u6-l2**：几个关键 lowering Pass 的作用，尤其是 `LayoutInference`（给 fragment/shared 缓冲推导物理布局）和 `LowerTileOp`（把 `T.gemm`/`T.copy` 等 tile op 占位展开成 `mma_sync`/`cp.async` 等底层 intrinsic）。

本讲要解决的核心痛点是：**编译器是一个黑盒**。当你写了一个 GEMM，性能不对或行为异常，你看到的是「Python 进、cubin 出」，中间五十多个 Pass 把 IR 改得面目全非。你需要一把「显微镜」：

- 想知道**某个 Pass 把 IR 改成了什么样** → 用编译期 IR 差分工具（lower trace / pass_diff / pass_visualizer）。
- 想知道**kernel 实际跑起来时某个变量的值** → 用运行期 `T.print`。

两类工具正交：前者看的是「编译器在做什么」，后者看的是「硬件在算什么」。

> 术语提示：**monkey-patch（猴子补丁）** 指在运行时替换某个对象（这里是 `tvm.ir.transform.Pass.__call__`）的方法。本讲的编译期工具全部基于 monkey-patch 实现「不改一行用户代码、打开一个开关就能观测」的效果。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tilelang/__init__.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/__init__.py) | 包入口。在 import 时按环境变量决定是否安装 lower_trace / pass_diff 钩子。 |
| [tilelang/env.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/env.py) | 集中定义 `TL_LOWER_TRACE` / `TILELANG_PASS_DIFF` 等环境变量与解析方法。 |
| [tilelang/utils/pass_diff_hook.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/utils/pass_diff_hook.py) | `TILELANG_PASS_DIFF` 钩子：最简形态的逐 Pass IR 差分，零开销设计。 |
| [tilelang/utils/pass_diff.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/utils/pass_diff.py) | `pass_diff()` 编程式 API 与 HTML 模板（被 pass_diff_hook 复用）。 |
| [tilelang/tools/lower_trace/core.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tools/lower_trace/core.py) | lower trace 主力实现：三层 hook（Pass / Pipeline / codegen FFI）、编辑重编译工作流。 |
| [tilelang/tools/lower_trace/\_\_init\_\_.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tools/lower_trace/__init__.py) | `enable/disable/reset` 全局钩子与 `lower_trace()` 一次性 API。 |
| [tilelang/tools/lower_trace/diff.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tools/lower_trace/diff.py) | 终端 unified diff 与 GitHub 风格 side-by-side HTML 差分生成。 |
| [tilelang/tools/pass_visualizer/core.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tools/pass_visualizer/core.py) | 加载用户 kernel、重建 CUDA prologue Pass 列表、把 PrimFunc 渲染成 SBlock 结构树。 |
| [tilelang/tools/pass_visualizer/viewer.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tools/pass_visualizer/viewer.py) | CLI 入口与交互式 HTML 浏览器：逐 Pass 高亮「新增/删除」的结构树行。 |
| [tilelang/cuda/language/print.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/language/print.py) | `T.print`：运行期设备端调试打印，按 buffer scope 分发到不同的 `debug_print_*` extern 调用。 |

## 4. 核心概念与源码讲解

### 4.1 tilelang.utils.pass_diff_hook —— TILELANG_PASS_DIFF 编译期逐 Pass 差分钩子

#### 4.1.1 概念说明

`pass_diff_hook` 是 tilelang 最早的「逐 Pass IR 差分」机制，由环境变量 `TILELANG_PASS_DIFF` 触发。它的思路极其直白：

- TVM 里每个 Pass 都是一个可调用对象，真正的执行入口是 `Pass.__call__(self, mod) -> mod`。
- 我们在 import tilelang 时把 `Pass.__call__` 替换成自己的 `_patched_call`：在调用**原始** `__call__` 前抓一份 `mod.script()`，调用后再抓一份，对两份文本做 unified diff，打印到终端或累计成 HTML。
- 当环境变量没开时，**根本不打补丁**，于是零开销。

这套机制最关键的设计目标就是「**默认零开销**」：生产构建和基准测试里这个钩子必须完全不可见。学习目标里专门点了这个设计，我们会在源码精读里看清它如何实现。

它是后续 `lower_trace`（4.2 节）的「前身与最简形态」——后者复用了同样的 `Pass.__call__` hook 点，并叠加了「phase 上下文、codegen 捕获、编辑重编译」等能力。官方文档明确建议新用户直接用 `TL_LOWER_TRACE`。

#### 4.1.2 核心流程

```text
import tilelang
   │
   ├─ __init__.py 读取 env.get_pass_diff_mode()
   │     └─ None（默认）→ install_pass_diff_hook() 立即 return，零开销
   │        非 None（terminal/html/both）→ 继续
   │
   ├─ install_pass_diff_hook():
   │     1. 保存原始 Pass.__call__
   │     2. 预加载差分工具（_compute_diff / _count_changes / _generate_html …）
   │     3. Pass.__call__ = _patched_call
   │     4. 若 html 模式：准备输出目录 + 注册 atexit 写最终报告
   │
   └─ 此后每次编译、每个 Pass 执行时：
         _patched_call(self, mod):
            before = mod.script()
            result = 原始 __call__(self, mod)      # 真正跑 Pass
            after  = result.script()
            diff   = compute_diff(before, after)
            ── terminal 模式：彩色打印
            ── html 模式：累计到 _html_steps，每个 Pass 后即时 flush
            return result                          # 透明地返回，不影响编译
```

注意 `_patched_call` 是**完全透明**的：它一定调用原始 `__call__` 并把结果原样返回，差分只是「侧观测」。所以开启钩子不会改变编译结果，只增加观测开销。

#### 4.1.3 源码精读

**① 入口与零开销判定**：钩子的安装发生在 `tilelang/__init__.py`，非 light import 时一律调用，但真正是否打补丁由模式决定：

[ tilelang/__init__.py:230-234 ](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/__init__.py#L230-L234) —— import 时安装 pass_diff 钩子（light import 下跳过，使无 GPU 机器与 AutoDD CLI 也能快速启动）。

[ tilelang/utils/pass_diff_hook.py:130-138 ](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/utils/pass_diff_hook.py#L130-L138) —— **零开销的关键**：`env.get_pass_diff_mode()` 返回 `None` 时直接 `return`，连 `Pass.__call__` 都不碰。这就是「关闭时与无此功能完全等价」的保证。

```python
mode = env.get_pass_diff_mode()
if mode is None:
    return  # disabled — zero overhead from here on
```

模式从哪来？看环境变量的解析：

[ tilelang/env.py:466-475 ](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/env.py#L466-L475) —— `get_pass_diff_mode()` 把 `TILELANG_PASS_DIFF` 的字符串归一化：`0/off/false/no/""` → `None`；`1/true/yes/on/terminal` → `"terminal"`；`html`/`both` 原样返回。

**② 打补丁**：

[ tilelang/utils/pass_diff_hook.py:161-164 ](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/utils/pass_diff_hook.py#L161-L164) —— 保存原始 `Pass.__call__`，再替换为 `_patched_call`。`_original_call` 为 `None` 还用作「是否已安装」的幂等标志。

**③ 透明的 before/after 捕获**：

[ tilelang/utils/pass_diff_hook.py:66-82 ](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/utils/pass_diff_hook.py#L66-L82) —— `_patched_call` 的核心：抓 `before_script` → 跑原始 Pass → 抓 `after_script`。两段 `try/except` 保证即使 `mod.script()` 失败（个别模块不可序列化）也不会让编译崩掉，而是退化成 `"<failed to capture>"`。

[ tilelang/utils/pass_diff_hook.py:84-117 ](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/utils/pass_diff_hook.py#L84-L117) —— 算 diff 行数、构造 `step` dict、按模式分别走终端彩色打印与 HTML 累计。`html`/`both` 模式下**每个 Pass 都即时 flush** 一次 HTML（`generate_html(_html_steps, _html_path)`），这样即使进程崩溃，已跑过的 Pass 报告也还在磁盘上。

**④ 性能优化细节**：注意 `_patched_call` 里用的 `compute_diff`、`count_changes` 等都是模块级 `_diff_utils` 字典里的引用：

[ tilelang/utils/pass_diff_hook.py:140-159 ](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/utils/pass_diff_hook.py#L140-L159) —— 在 `install` 时**一次性**从 `tilelang.utils.pass_diff` 预加载所有差分工具到 `_diff_utils` 字典。这样高频的 `_patched_call` 里不再有 `import`/属性查找，把每个 Pass 的额外开销压到最低。

**⑤ 卸载**：

[ tilelang/utils/pass_diff_hook.py:189-205 ](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/utils/pass_diff_hook.py#L189-L205) —— `uninstall_pass_diff_hook()` 把 `Pass.__call__` 还原、清空所有状态。补丁必须完全可逆，这是观测工具的基本要求（避免污染后续基准测试）。

#### 4.1.4 代码实践

**实践目标**：用最少配置体验 `TILELANG_PASS_DIFF` 的终端彩色差分，直观看到某个 Pass 改了什么。

**操作步骤**：

1. 准备一个最简 GEMM 脚本 `gemm_debug.py`（基于 `examples/quickstart.py` 改写，只保留编译、不跑性能）：

   ```python
   import tilelang
   import tilelang.language as T

   @tilelang.jit
   def matmul(A, B, block_M: int, block_N: int, block_K: int):
       M, N, K = T.const("M, N, K")
       A: T.Tensor((M, K), T.float16)
       B: T.Tensor((K, N), T.float16)
       C = T.empty((M, N), T.float16)
       with T.Kernel(T.ceildiv(N, block_N), T.ceildiv(M, block_M), threads=128) as (bx, by):
           A_shared = T.alloc_shared((block_M, block_K), T.float16)
           B_shared = T.alloc_shared((block_K, block_N), T.float16)
           C_local  = T.alloc_fragment((block_M, block_N), T.float32)
           T.clear(C_local)
           for ko in T.Pipelined(T.ceildiv(K, block_K), num_stages=3):
               T.copy(A[by * block_M, ko * block_K], A_shared)
               T.copy(B[ko * block_K, bx * block_N], B_shared)
               T.gemm(A_shared, B_shared, C_local)
           T.copy(C_local, C[by * block_M, bx * block_N])
       return C

   matmul.compile(M=512, N=512, K=512, block_M=128, block_N=128, block_K=32)
   ```

2. 用环境变量打开终端差分：

   ```bash
   TILELANG_PASS_DIFF=terminal python3 gemm_debug.py
   ```

**需要观察的现象**：

- 终端会逐个 Pass 打印 `==== Pass N/∞: <name> ====`，下面是彩色 unified diff（`+` 绿、`-` 红、`@@` 青）。
- 绝大多数 Pass 标 `(no changes)`（该 Pass 对这份 IR 的文本无影响），少数会显示 `+N insertion(s), -M deletion(s)`。
- 找到名为 `Simplify`、`LayoutInference`、`LowerTileOp`、`InjectSoftwarePipeline` 的步骤，它们通常有可观察的差分。

**预期结果**：你能在终端看到一条「逐 Pass」的差分流，证明钩子透明地记录了编译全过程。具体每个 Pass 改了几行取决于 kernel 规模，**待本地验证**确切行数。

> 注意：`TILELANG_PASS_DIFF` 与 4.2 的 `TL_LOWER_TRACE` 都 patch 同一个 `Pass.__call__`，**不要同时开启**，否则会层层包裹、重复捕获。

#### 4.1.5 小练习与答案

**练习 1**：为什么说 `TILELANG_PASS_DIFF=0 python gemm_debug.py` 是「零开销」？请结合源码说明。

**参考答案**：`get_pass_diff_mode()` 把 `"0"` 解析为 `None`，`install_pass_diff_hook()` 在第一行就 `return`，从未替换 `Pass.__call__`，也从未 import 差分工具。因此 Pass 执行路径与「没有这个功能」时完全一致，没有任何额外指令。

**练习 2**：`_patched_call` 里为什么把 `before/after` 的捕获都包在 `try/except` 里？

**参考答案**：观测工具不能让编译崩掉。某些 IRModule 的 `.script()` 在边缘情况下可能抛异常（如不可序列化的属性）；捕获后退化成占位字符串 `"<failed to capture>"`，差分仍能进行，而真正的 Pass 执行（`_original_call(self, mod)`）不受影响、异常照常向上抛。

---

### 4.2 tilelang.tools.lower_trace —— 主力 IR 差分与 codegen 捕获工具

#### 4.2.1 概念说明

`lower_trace` 是 pass_diff 的「严格超集」，也是官方推荐的主力调试工具。它在 pass_diff 的基础上增加了：

- **Phase 上下文**：每个 Pass 会被打上它所属流水线阶段的标签（如 `pipeline_c`、`phase1_...`），让你能一眼看出某个 Pass 属于哪个后端阶段。
- **Codegen 捕获**：最后一棒「TIR → C/CUDA/HIP 源码」也被记录，生成的源码落盘到 `codegen.cpp`，可检查甚至**编辑后重编译**。
- **原始 `.tir` 落盘**：每个 Pass 的 before/after IR 都写成 `.tir` 文件，按 phase + 全局序号命名。
- **崩溃安全的增量 HTML**：每跑完一个 Pass 就 flush 一次 HTML，即使 `SIGKILL` 也能保留已跑部分。
- **多轮累积**：同一进程里多次编译会用 `run2_`、`run3_` 前缀区分，方便横向对比。

它由环境变量 `TL_LOWER_TRACE` 触发，也提供 `enable()/disable()/reset()` 编程式 API 和 `lower_trace()` 一次性 API。

#### 4.2.2 核心流程

lower_trace 安装**三层 hook**，覆盖编译链路的三个关键点：

```text
① Pass.__call__ hook        —— 抓「每个 Pass」的 before/after IR
② PassPipeline.lower hook   —— 给「一整条流水线」打 phase 标签
③ codegen FFI hook           —— 抓「TIR → 源码」最后一棒，驱动编辑重编译
```

数据流：

```text
Pass 执行 → _traced_pass_call:
   phase = _current_phase or "unscoped"
   before = str(mod)
   result = 原始 Pass(mod)
   after  = str(result)
   ── 计算 +/- 行数 → 构造 LowerRecord → 追加到 _records
   ── _save_raw_files: 写 <phase>/<idx>_<name>_before.tir 与 _after.tir
   ── _incremental_flush_html: 重写 report.html（复用 section_cache，O(n) 总开销）
   return result
```

phase 标签来自第二层 hook：当 `PassPipeline.lower` 被调用时，`_traced_pipeline_lower` 把 `_current_phase` 设成 `pipeline_<name>`，于是这窗口内所有 Pass 都带上该标签；窗口外的 Pass（如 pre-pipeline 模块 Pass）标 `unscoped`。

输出目录结构（来自官方文档）：

```text
<TL_LOWER_TRACE_DIR>/<script_name>/
├── report.html                 # 符号链接 → 最新一次 run 的报告
├── codegen.cpp                 # 可编辑的 codegen 源码工作副本
├── codegen.cpp.original        # 基线快照（编辑重编译用）
├── codegen.cpp.latest          # 本次 codegen 的真实输出
└── .run_records/run_<时间戳>_<pid>/
    ├── report.html
    ├── pipeline_c/             # 每个 phase 一个子目录
    │   ├── 00_BindTarget_before.tir
    │   └── 00_BindTarget_after.tir
    └── codegen/
        ├── 42_codegen_before.tir
        └── 42_codegen_after.cpp
```

#### 4.2.3 源码精读

**① 配置与模式判定**：

[ tilelang/tools/lower_trace/core.py:220-256 ](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tools/lower_trace/core.py#L220-L256) —— `_parse_lower_trace_mode` 把 `TL_LOWER_TRACE` 的值归一化（`1/on` → `"html"`，`terminal`/`html`/`both` 原样）。`_get_mode` 优先用编程式 override，否则回退到 `env.get_lower_trace_mode()`。`_should_gen_html()` / `_should_print_terminal()` 按模式分流。

**② Pass hook 的核心**：

[ tilelang/tools/lower_trace/core.py:400-449 ](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tools/lower_trace/core.py#L400-L449) —— `_traced_pass_call` 的主干：未启用时直接走原路径（`_is_trace_enabled()` 短路）；启用时抓 `before_text = str(mod)`、跑原始 Pass、抓 `after_text`，用 `before_text != after_text` 判断是否变化。

[ tilelang/tools/lower_trace/core.py:453-493 ](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tools/lower_trace/core.py#L453-L493) —— 用 `difflib.SequenceMatcher` 统计 `+`/`-` 行数，构造 `LowerRecord` 追加到 `_records`，落盘，并在终端打印一行 `[lower_trace] <phase>/<idx>_<name>: CHANGED|NO-OP`。失败分支（426-446）会构造 `STATUS_FAILED` 记录后再 `raise`，保证崩溃也能被记录。

**③ Phase 上下文 hook**：

[ tilelang/tools/lower_trace/core.py:740-764 ](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tools/lower_trace/core.py#L740-L764) —— `_traced_pipeline_lower`：把 `_current_phase` 设成 `pipeline_<self.name>` 再调用原始 `PassPipeline.lower`，结束于 `finally` 风格恢复。注意记录是**运行时追加**（不预注册），所以被条件跳过的 Pass（如 `LetInline`）不会留下幽灵空槽。

**④ 记录的数据结构**：

[ tilelang/tools/lower_trace/core.py:107-121 ](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tools/lower_trace/core.py#L107-L121) —— `LowerRecord` dataclass：`phase/name/index/before_text/after_text/changed/add_lines/del_lines/status/error_msg`。`status` 取自模块顶部常量 `STATUS_COMPLETED/FAILED/SKIPPED/CODEGEN`。

**⑤ Codegen 捕获与编辑重编译**：

[ tilelang/tools/lower_trace/core.py:817-880 ](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tools/lower_trace/core.py#L817-L880) —— `_wrap_codegen_ffi` 的文档注释完整说明了「三文件协作」的编辑重编译工作流。核心思路是三方比较（基线 `.original` / 工作副本 `codegen.cpp` / 本次输出 `.latest`），据此决定是 `REGENERATED`、`PATCHED`（注入用户编辑）、`SYNCED` 还是 `CONFLICT`（冲突，备份后用新 codegen 覆盖）。

[ tilelang/tools/lower_trace/core.py:187-199 ](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tools/lower_trace/core.py#L187-L199) —— `_SOURCE_ONLY_CODEGEN_FFIS`：只有「产物只通过 `get_source()` 消费」的 codegen FFI（如 `*_without_compile`、`tilelang_c`、`webgpu`、`tilelang_ascend`）才能把用户编辑后的源码真正重新编译；全编译型 FFI（`tilelang_cuda` 等）只能记录编辑用于 diff，不能重编译。

[ tilelang/tools/lower_trace/core.py:791-814 ](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tools/lower_trace/core.py#L791-L814) —— `_make_patched_source_module`：用真正的 `CSourceModuleCreate` 把编辑后的源码包成合法的 TVM Module，因为 FFI 返回值只能跨边界传递 TVM 识别的类型（纯 Python proxy 不行）。

**⑥ enable / disable**：

[ tilelang/tools/lower_trace/core.py:1135-1166 ](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tools/lower_trace/core.py#L1135-L1166) —— `enable()` 安装三层 hook：先 patch `Pass.__call__`，再逐个 wrap `_CODEGEN_FFI_NAMES` 里的 codegen FFI，最后 patch `PassPipeline.lower`（新架构）或回退到 phase 函数（旧架构）。幂等。

[ tilelang/tools/lower_trace/core.py:1225-1281 ](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tools/lower_trace/core.py#L1225-L1281) —— `disable()` 把三层 hook 全部还原、清空状态，完全可逆。

**⑦ 一次性 API `lower_trace()`**：

[ tilelang/tools/lower_trace/\_\_init\_\_.py:47-127 ](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tools/lower_trace/__init__.py#L47-L127) —— 不安装任何全局 hook，只在给定 IRModule 上**手动**跑一组 Pass 链并出报告。适合「我就想看这几个 Pass 的效果」的精细场景，且不会污染进程内其他编译。

**⑧ 差分与 HTML**：

[ tilelang/tools/lower_trace/diff.py:353-394 ](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tools/lower_trace/diff.py#L353-L394) —— `unified_diff`：基于 `difflib.unified_diff`，可选 ANSI 着色，是终端模式的核心。

[ tilelang/tools/lower_trace/diff.py:166-350 ](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tools/lower_trace/diff.py#L166-L350) —— `_make_diff_html`：生成 GitHub 风格的 side-by-side 差分表格，含字符级 inline 高亮、可折叠上下文（`↑↓ Expand` 按钮）。这是 HTML 报告里每个 Pass 的视觉主力。

#### 4.2.4 代码实践（本讲核心实践）

**实践目标**：开启 lower trace 编译一个 GEMM，在生成的报告里定位 `LayoutInference` 这个 Pass，对比它前后的 IR；再用一次性 API 单独看一个 Pass 的效果。

**操作步骤**：

1. 复用 4.1.4 的 `gemm_debug.py`，用环境变量打开 HTML 报告：

   ```bash
   TL_LOWER_TRACE=1 python3 gemm_debug.py
   ```

   > 关键：`TL_LOWER_TRACE` 必须在 **import tilelang 之前**设置，因为 `__init__.py` 在 import 时就读取它（见 [ tilelang/__init__.py:222-225 ](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/__init__.py#L222-L225)）。

2. 编译完成后，打开报告：

   ```bash
   # 典型路径（macOS 用 open，Linux 用 xdg-open）
   open tmp/lower_trace_dir/gemm_debug/report.html
   ```

   或终端模式直接看彩色差分：

   ```bash
   TL_LOWER_TRACE=terminal python3 gemm_debug.py
   ```

3. **定位 LayoutInference**：

   - 在 HTML 左侧边栏按名字找 `LayoutInference`（状态点 `●` 表示有改动），或在 `tmp/lower_trace_dir/gemm_debug/.run_records/run_*/pipeline_c/` 目录下找形如 `NN_LayoutInference_before.tir` / `NN_LayoutInference_after.tir` 的两个文件。
   - 直接用文本工具对比这两个 `.tir` 文件即可，无需 HTML。

4. **用一次性 API 单独看一个 Pass**（不安装全局钩子）：

   ```python
   # trace_one_pass.py
   from tilelang.tools import lower_trace as lt
   import tilelang
   import tilelang.language as T
   import tilelang.transform as transform

   @tilelang.jit
   def matmul(A, B, block_M: int, block_N: int, block_K: int):
       # ... 同 4.1.4 的 kernel 体 ...
       return C

   func = matmul.get_tir(M=512, N=512, K=512, block_M=128, block_N=128, block_K=32)
   lt.lower_trace(func, transform.Simplify(), mode="terminal")
   ```

**需要观察的现象**：

- `LayoutInference` **之前**的 IR：`C_local` 这类 fragment 缓冲的分配处**没有** `layout_map` 之类的布局注解，循环结构是用户书写的 tile 级形态。
- `LayoutInference` **之后**的 IR：fragment 缓冲上出现了布局注解（描述逻辑下标到物理位置/线程的映射），但**循环结构基本不变**——这个 Pass 只挂注解、不展开循环（这是 u6-l2 讲过的「Strict/Common/Free 三级约束传播」的结果）。
- 真正「展开」发生在更靠后的 `LowerTileOp`：那里你会看到 `T.gemm` 占位消失、出现 `ptx_mma`/`cp.async` 之类底层 intrinsic。

**预期结果**：

- HTML 报告左侧应能看到几十个 Pass，其中 `LayoutInference` 标 `changed`、显示 `+N -M` 行数变化。
- `LayoutInference` 的 before/after `.tir` 差异集中在缓冲注解区域，循环骨架几乎不动。
- 具体的 Pass 总数与行数取决于 target 与 kernel 规模，**待本地验证**。

> 提示：lower trace 捕获的是 `IRModule.script()` 的**文本**变化。「文本变了」是 Pass 起作用的证据，但不等同于「语义变了」或「性能变了」——这是官方文档反复强调的注意事项。

#### 4.2.5 小练习与答案

**练习 1**：`enable(mode="both")` 之后忘了调 `disable()`，会对后续编译造成什么影响？如何缓解？

**参考答案**：钩子会一直挂着，进程内后续所有编译（包括无关 kernel）都会被捕获，每个 Pass 都要抓两份 `script()` 并 flush HTML，开销显著。缓解方式：调试完调 `lt.disable()` 彻底卸载钩子；或在编译多个 kernel 时用 `lt.reset()` 清空记录、让每个 kernel 得到独立报告（记录默认会累积并用 `run2_`/`run3_` 前缀区分）。

**练习 2**：为什么 `_traced_pass_call` 要在记录里保留 `STATUS_FAILED` 分支，而不是让异常直接抛出？

**参考答案**：为了让「失败本身」也可观测。如果某个 Pass 抛异常，先构造一条 `STATUS_FAILED` 的 `LowerRecord`（记录 `before_text` 与错误信息）再 `raise`，这样 HTML 报告里会留下一个红色失败节点，并展示崩溃前的 IR，帮助定位是哪个 Pass、在哪份 IR 上炸的。这是「崩溃安全」设计的一部分。

**练习 3**：编辑重编译工作流里，为什么只有 `*_without_compile` 这类「source-only」FFI 才能真正重编译用户编辑？

**参考答案**：全编译型 FFI（如 `tilelang_cuda`）在 codegen 阶段就把 TIR 编译成了 PTX/cubin 二进制，下游通过 `import_module` 消费的是这个二进制——源码只是附带的展示，改源码不影响二进制。而 source-only FFI（`nvrtc`/`cython`/`cutedsl` 后端用的 `*_without_compile`）只产出源码字符串，真正的编译发生在运行时由 NVRTC/Cython/CuTeDSL 完成，所以把编辑后的源码塞回去（经 `CSourceModuleCreate` 包成合法 Module 跨过 FFI 边界）就能被重新编译。

---

### 4.3 tilelang.tools.pass_visualizer —— SBlock 结构树逐 Pass 可视化

#### 4.3.1 概念说明

lower trace / pass_diff 比较的是 **TIR 文本**（`script()` 输出），对「这一 Pass 在结构上到底插了什么节点」并不直观。`pass_visualizer` 换了一个视角：它把每个 Pass 之后的 IR 渲染成 **SBlock 结构树**（按 `PrimFunc → buffer_map → body → SBlock → iter_vars/reads/writes/annotations → 子语句` 的树状缩进），再把相邻两棵树的行做 diff——**这个 Pass 新增的行高亮成绿色、删除的行灰红删除线**。

更进一步，它对每一行做了**语义高亮**：

- tile op（`T.gemm`/`T.copy`/`T.reduce`/`T.fill`/`T.cumsum` …）——橙色块；
- 同步原语（`mbarrier_wait_parity`/`ptx_arrive_barrier` …）——紫色块；
- 已 lowering 的硬件 intrinsic（`ptx_mma`/`ptx_ldmatrix`/`tma_load` …）——蓝色块。

于是你可以在浏览器里用 `↑/↓` 键逐 Pass 翻阅，**眼睁睁看着 `T.gemm` 在 `LowerTileOp` 那一步变成 `ptx_mma`**——这是文本 diff 难以传达的「结构演化」感。

它不是 monkey-patch，而是一个独立的 CLI：你把 kernel 文件喂给它，它**自己重建一条 CUDA prologue Pass 流水线**，逐步跑、逐步截图。

#### 4.3.2 核心流程

```text
viewer.main(path, --factory, --target, --set K=V, --out):
   1. load_user_module(path)             # importlib 加载用户文件
   2. discover_jit_kernels(module)        # 找到文件里所有 @tilelang.jit
   3. kernel_to_tir(kernel, **kwargs)     # 展开成未 lower 的 PrimFunc
   4. build_module(func, target)          # 包成 IRModule + 解析 target
   5. PreLowerSemanticCheck(mod)          # 跑一遍 lower 前语义检查
   6. build_pass_stages(target)           # 重建 CUDA prologue Pass 列表
   7. 逐个跑 Pass，每步：
        before = str(mod); mod = pass(mod); after = str(mod)
        cur_tree = capture_tree(mod)      # inspect_structure 的输出
        rows = diff_rows(prev_tree, cur_tree)
   8. emit_html / emit_txt → 写出 HTML + 同名 .txt
```

注意第 6 步：`build_pass_stages` **手动复刻**了 `tilelang/cuda/pipeline.py` 里 `CUDAPassPipelineBodyPrologue` 的 Pass 顺序（见 4.3.3），这样 viewer 跑的 Pass 序列就是真实 CUDA 后端的 prologue。

#### 4.3.3 源码精读

**① CLI 入口**：

[ tilelang/tools/pass_visualizer/viewer.py:439-468 ](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tools/pass_visualizer/viewer.py#L439-L468) —— `main`：解析 `path/--factory/--target/--set/--out`，读源码、调 `build_pass_data`、写 HTML 与同名 `.txt`。

**② 逐 Pass 截图与行级 diff**：

[ tilelang/tools/pass_visualizer/viewer.py:157-219 ](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tools/pass_visualizer/viewer.py#L157-L219) —— `build_pass_data`：stage 0 是源码、stage 1 是 `(input)`（pipeline 输入），之后逐 Pass 跑、用 `_diff_rows` 把相邻两棵结构树 diff 成 `equal/add/del` 行。`with resolved_target:` 是因为 `LayoutInference` 之后的 Pass 依赖 `Target.Current()`。

[ tilelang/tools/pass_visualizer/viewer.py:125-154 ](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tools/pass_visualizer/viewer.py#L125-L154) —— `_diff_rows`：`difflib.SequenceMatcher` 把 prev→cur 的行打成 `equal/insert/delete/replace`，并预渲染高亮 HTML 与 tile-op 标志。

**③ 重建 CUDA Pass 列表**：

[ tilelang/tools/pass_visualizer/core.py:94-142 ](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tools/pass_visualizer/core.py#L94-L142) —— `build_pass_stages`：手工拼出 `BindTarget → MaterializeKernelLaunch → … → PipelinePlanning → InjectSoftwarePipeline → Simplify → LayoutInference → LowerTileOp → …`，与真实 CUDA prologue 一致（注释里标注了对应 `pipeline.py` 的行号）。

**④ 结构树渲染**：

[ tilelang/tools/pass_visualizer/core.py:368-392 ](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tools/pass_visualizer/core.py#L368-L392) —— `inspect_structure`：从 `params / buffer_map / attrs / body` 自顶向下打印 PrimFunc，body 用 `_walk_stmt` 递归。

[ tilelang/tools/pass_visualizer/core.py:301-366 ](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tools/pass_visualizer/core.py#L301-L366) —— `_walk_stmt`：按节点类型（`SBlockRealize/SBlock/AttrStmt/SeqStmt/For/IfThenElse/Evaluate/BufferStore`）分发。其中 `Evaluate` 分支会专门识别 `tl.tileop.*` 调用并按字段名展开（如把 `T.gemm` 的 16 个位置参数对应到 `a_region/b_region/c_region/transA/…`），这就是结构树里 tile op 显示得这么清楚的原因——见 `_TILEOP_FIELDS`（core.py:242-268）。

**⑤ 三类算子高亮**：

[ tilelang/tools/pass_visualizer/viewer.py:62-96 ](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tools/pass_visualizer/viewer.py#L62-L96) —— `_TILE_OPS`/`_SYNC_OPS`/`_HW_OPS` 三个白名单与对应正则，`_highlight` 在服务端把每一行染成安全 HTML（inline style，不依赖外部 CSS）。

**⑥ 懒加载**：

[ tilelang/tools/pass_visualizer/\_\_init\_\_.py:9-21 ](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tools/pass_visualizer/__init__.py#L9-L21) —— 只导出 `core` 的几个函数，**故意不**导入 `viewer`（`build_pass_data`/`emit_html` 等），因为 `viewer` 体积大且 `python -m ...viewer` 时预导入会触发 `RuntimeWarning`。需要 viewer 时从 `tilelang.tools.pass_visualizer.viewer` 直接导入。

#### 4.3.4 代码实践

**实践目标**：用 pass_visualizer 生成一个 GEMM 的交互式 Pass 浏览器，在浏览器里翻到 `LowerTileOp`，观察 `T.gemm` 变成硬件 intrinsic 的那一刻。

**操作步骤**：

1. 用仓库自带的示例 kernel（已带 bias + ReLU epilogue）：

   ```bash
   python -m tilelang.tools.pass_visualizer.viewer \
     tilelang/tools/pass_visualizer/examples/gemm_relu.py \
     --set M=1024 --set N=1024 --set K=1024 \
     --set block_M=128 --set block_N=128 --set block_K=32 \
     --out gemm_relu_passes.html
   ```

2. 打开生成的 HTML，或查阅同名 `gemm_relu_passes.txt`（纯文本、可 grep）：

   ```bash
   open gemm_relu_passes.html
   ```

**需要观察的现象**：

- 左栏是按序排列的 Pass 列表（含 `source code`、`(input)` 与各 Pass），右侧是当前选中 Pass 的结构树。
- 用 `↑/↓` 翻到 `LowerTileOp`：右侧应出现大量绿色 `add` 行（新增的 `ptx_mma`/`cp.async` 等），并能看到原本橙色的 `T.gemm`/`T.copy` tile op 行变成灰红删除线（被这个 Pass 消费掉）。
- 翻到 `LayoutInference`：fragment 缓冲的 `annotations` 区域出现新增的布局映射行，循环骨架行多为 `equal`。

**预期结果**：你能在浏览器里逐 Pass 看到结构树的演化，`LowerTileOp` 那一步 tile op → 硬件 intrinsic 的转换清晰可见。具体行内容与 target 相关（SM80/SM90 选用的指令不同），**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：pass_visualizer 和 lower_trace 都做「逐 Pass diff」，二者最本质的区别是什么？

**参考答案**：lower_trace 比较 `IRModule.script()` 的**原始文本**，是无侵入的全局钩子，覆盖整条流水线（含 codegen）；pass_visualizer 比较的是 `inspect_structure` 渲染出的**结构树文本**（只到 CUDA prologue），并做了语义高亮（tile op / sync / hw intrinsic 三类），是独立 CLI、不挂全局钩子。前者适合「全面记录与离线分析」，后者适合「直观理解 Pass 在结构层面改了什么」。

**练习 2**：`build_pass_data` 里为什么要 `with resolved_target:` 包住 `transform(mod)`？

**参考答案**：`LayoutInference` 之后的若干 Pass 需要查询当前 target（如根据 arch 选指令），TVM 的惯例是通过 `Target.Current()` 读取「上下文中的 target」。`with resolved_target:` 把 target 压入栈，保证这些 Pass 能正确取到；否则会因找不到当前 target 而报错或行为异常。

---

### 4.4 T.print —— 运行期设备端调试输出

#### 4.4.1 概念说明

前面三个工具都是**编译期**的——它们看的是「编译器把 IR 变成了什么」。但有时候你只想知道：**kernel 跑起来时，这个循环变量到底取了哪些值？这个缓冲里装的是不是 NaN？**

`T.print` 就是干这个的。它是 tilelang CUDA 方言里的运行期调试打印，最终生成对设备端 `debug_print_var` / `debug_print_buffer_value` / `debug_print_msg` 这几个 extern 句柄的调用，由 C++ 运行时翻译成 `printf` 一类的设备输出。

关键区别：

| 维度 | 编译期工具（lower trace / pass_diff / visualizer） | `T.print` |
| --- | --- | --- |
| 时机 | 编译时 | kernel 运行时 |
| 看什么 | IR / 结构树如何变形 | 变量 / 缓冲的实际数值 |
| 是否需要 GPU | 否（只到 codegen 源码即可） | 是（要真跑 kernel） |
| 代价 | 增加编译时间 | 增加 kernel 运行时间，可能大量输出 |

#### 4.4.2 核心流程

`T.print` 是一个**普通函数**（不是 macro 本身），按 `obj` 的类型分发：

```text
T.print(obj, msg="", warp_group_id=0, warp_id=0):
   obj 是 tirx.Buffer:
      按 buffer.scope() 分发：
        local          → print_local_buffer_with_condition   (每线程都打)
        local.fragment → print_fragment_buffer_with_condition (只 main_lane 线程打)
        shared/shared.dyn → print_shared_buffer_with_condition (只 main_lane 线程打)
        global         → print_global_buffer_with_condition  (每线程都打)
   obj 是 tirx.PrimExpr:
      → print_var → tirx.call_extern("handle","debug_print_var", msg, var)
   obj is None:
      → print_msg → tirx.call_extern("handle","debug_print_msg", msg)
```

注意 fragment/shared buffer 默认只在 `main_lane = warp_group_id*128 + warp_id*32` 且 `tx==main_lane, ty==0, tz==0` 的线程打印，避免一个 block 里上百个线程各打一遍造成输出爆炸；而标量表达式 `print_var` 不做线程过滤，因为循环变量这类标量在每个线程里同值（或你本就想看分布）。

#### 4.4.3 源码精读

**① 暴露为 T.print**：

[ tilelang/cuda/language/\_\_init\_\_.py:55-56 ](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/language/__init__.py#L55-L56) —— `from .print import *` 把 `print` 导入 CUDA 方言；由于 `tilelang.language` 默认就是 CUDA 方言，所以 `import tilelang.language as T` 后即可用 `T.print`。

**② 标量打印**：

[ tilelang/cuda/language/print.py:16-27 ](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/language/print.py#L16-L27) —— `print_var` 是 `@macro`，展开成 `tirx.call_extern("handle", "debug_print_var", msg, var)`。`@macro`（来自 `tilelang.language.common`）让这个 DSL 原语在 builder 上下文里就地展开成 TIR。

**③ 主分发函数**：

[ tilelang/cuda/language/print.py:128-207 ](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/language/print.py#L128-L207) —— `print` 函数本体：按 `isinstance(obj, tirx.Buffer / tirx.PrimExpr / None)` 三分支分发。Buffer 分支里先 `get_thread_bindings()` 取 `tx,ty,tz`，算出 `main_lane`，再按 scope 调对应的条件打印宏。

**④ fragment buffer 打印要绕路到 shared**：

[ tilelang/cuda/language/print.py:78-98 ](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/language/print.py#L78-L98) —— `print_fragment_buffer_with_condition`：fragment（寄存器）不能被其它线程直接读，所以先 `alloc_shared` + `copy(buffer, smem)` 把它搬到 shared，再逐元素 `debug_print_buffer_value`。这是 fragment 调试比 shared/local 更贵的原因。

> 说明：以上是「示例代码」位置——它们是项目原有的 DSL 原语实现，不是本讲新写的。`debug_print_*` 句柄的 C++ 实现不在本讲源码范围内，行为是「在设备端按 `printf` 输出」。

#### 4.4.4 代码实践

**实践目标**：在 GEMM kernel 里用 `T.print` 打印一次循环变量 `ko`，运行 kernel 观察设备端输出。

**操作步骤**：

1. 在 4.1.4 的 kernel 体里，`T.Pipelined` 循环内加一行（注意：这会修改你本地的脚本，**不要改仓库源码**，只改你自己的 `gemm_debug.py`）：

   ```python
   for ko in T.Pipelined(T.ceildiv(K, block_K), num_stages=3):
       T.copy(A[by * block_M, ko * block_K], A_shared)
       T.copy(B[ko * block_K, bx * block_N], B_shared)
       T.gemm(A_shared, B_shared, C_local)
       T.print(ko, msg="ko=")          # 新增：打印循环变量
   ```

2. 这次需要**真正编译并运行** kernel（不再只 compile）。最简方式是用 `get_profiler()` 跑一次：

   ```python
   kernel = matmul.compile(M=512, N=512, K=512,
                           block_M=128, block_N=128, block_K=32)
   kernel.get_profiler().do_bench()    # 运行会触发设备端打印
   ```

3. 运行：

   ```bash
   python3 gemm_debug.py
   ```

**需要观察的现象**：

- 标准输出（或 stderr）里应出现形如 `ko= 0`、`ko= 1` … 的设备端打印，数量与网格规模、线程数相关。
- 因为 `print_var` 不过滤线程，同一个 `ko` 值会被 block 内多个线程重复打印，输出可能很嘈杂。

**预期结果**：能看到 `ko` 的取值序列从 0 递增到 `ceildiv(K, block_K)-1`（本例 K=512、block_K=32 → 0..15）。由于多线程重复打印，输出量大，**待本地验证**确切格式与重复次数。

> 降噪小技巧：若输出太吵，可以把 `T.print(ko)` 包在一个线程条件里（用 `get_thread_bindings()` 取 `tx` 判断），或只在 `bx==0 and by==0` 的 block 打印。打印 buffer 时优先用 fragment/shared 的条件版本（已默认只 main_lane 打印），避免刷屏。

#### 4.4.5 小练习与答案

**练习 1**：为什么打印 `local.fragment` buffer 要先 `copy` 到 shared？

**参考答案**：fragment 是线程私有的寄存器，只能被持有它的那个线程访问；而 `debug_print_buffer_value` 的逐元素读取需要按坐标遍历，跨线程读取 fragment 是非法的。先搬到 shared（block 内共享）再用统一的坐标遍历打印，才能得到完整的缓冲内容。代价是多一次 fragment→shared 搬运。

**练习 2**：`T.print(ko)` 和 lower trace 里看到的 `ko` 有什么本质不同？

**参考答案**：lower trace 里的 `ko` 是 TIR IR 中的一个**循环变量符号**，你看到的是「编译器如何处理这个名字」（比如流水线把它重写成 prologue/body/epilogue 三段）。`T.print(ko)` 输出的是 kernel **运行时**该变量依次取到的**实际整数值**。前者是编译期静态信息，后者是运行期动态数据。

---

## 5. 综合实践

把本讲的三个编译期工具和一个运行期工具串起来，完成一次完整的「GEMM 调试之旅」：

**任务**：给定一个 GEMM kernel（复用 `examples/quickstart.py` 或 4.1.4 的精简版），完成下面四步并记录每一步的发现。

1. **编译期全景（lower trace）**：用 `TL_LOWER_TRACE=1` 编译，在 `report.html` 里数一下 CUDA 后端总共跑了多少个 Pass，记下其中 `changed` 的有几个。在 `.run_records/.../pipeline_c/` 下找到 `LayoutInference` 的 before/after `.tir`，用一段话描述它改了什么、没改什么。

2. **结构演化（pass_visualizer）**：用 `python -m tilelang.tools.pass_visualizer.viewer` 对同一个 kernel 生成结构树 HTML。翻到 `LowerTileOp`，确认 `T.gemm` 是否在该步变成了带 `ptx_mma` 字样的硬件 intrinsic，并截图或抄录这一步新增的第一行。

3. **精细验证（一次性 lower_trace）**：用 `lt.lower_trace(func, [transform.Simplify(), transform.LayoutInference()], mode="terminal")` 单独跑这两个 Pass，确认 `Simplify` 对你的 kernel 是否产生文本变化（很可能 `(no changes)`），并与第 1 步的全量报告对照。

4. **运行期数值（T.print）**：在 kernel 里加 `T.print(ko, msg="ko=")` 并真跑一次，记下 `ko` 的取值范围，验证它等于 `range(ceildiv(K, block_K))`。

**交付物**：一份简短笔记，包含——Pass 总数与 changed 数、LayoutInference 的 textual 变化描述、LowerTileOp 结构树新增的首行、`ko` 的实际取值范围。

> 若本机无 GPU：第 1-3 步仍可完成（编译期工具不需要运行 kernel，部分步骤甚至可在 CPU target 下演示，但 Pass 序列会不同）；第 4 步必须真跑 kernel，无 GPU 时改为「源码阅读型实践」——阅读 `print.py` 解释 `T.print(C_local)`（fragment buffer）会展开成哪几条 TIR 语句。

## 6. 本讲小结

- tilelang 调试工具分两类：**编译期**（看 IR/结构树如何变形）与**运行期**（`T.print` 看实际数值），二者正交、配合使用。
- `TILELANG_PASS_DIFF`（`pass_diff_hook`）是最简形态的逐 Pass IR 差分，核心卖点是**零开销**：模式为 `None` 时 `install_pass_diff_hook()` 第一行就 return，不打任何 monkey-patch、不额外 import。
- `TL_LOWER_TRACE`（`lower_trace`）是 pass_diff 的严格超集与官方推荐主力：三层 hook（Pass / PassPipeline / codegen FFI）、phase 上下文、原始 `.tir` 落盘、增量崩溃安全 HTML，以及「三文件协作」的 codegen 编辑重编译工作流。
- `pass_visualizer` 换视角看结构：把每个 Pass 之后的 IR 渲染成 SBlock 结构树并做行级 diff + 三类算子高亮（tile op / sync / hw intrinsic），最适合直观理解 `LowerTileOp` 把 `T.gemm` 展开成 `ptx_mma` 的瞬间。
- `T.print` 是运行期设备端打印，按 buffer scope 分发到 `debug_print_*` extern 调用；fragment/shared 默认只在 main_lane 线程打印以防刷屏，fragment 还需先搬到 shared 才能逐元素读。
- 所有编译期钩子都**完全可逆**（`disable()`/`uninstall_*`），且捕获的是 `script()` 文本——「文本变了」不等于「语义或性能变了」。

## 7. 下一步学习建议

- **继续深挖 Pass 内部**：本讲只观测了 Pass 的输入输出，下一站读 [u9-l2 布局可视化与 Analyzer](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/)（`tools/plot_layout`、`tools/Analyzer`），看 LayoutInference 推导出的布局长什么样、Analyzer 如何用 Z3 做符号证明。
- **高级 intrinsics 调试**：[u9-l3 高级 CUDA intrinsics、TMA/cluster 与 iket](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/) 会讲 TMA/cluster/warpgroup，届时用 lower trace 观察 `tma_copy` 占位如何展开成 `tma_load` 会非常直观。
- **贡献新工具**：如果你想做自己的编译期观测工具，模仿 `pass_diff_hook.py` 的「保存原始 → 替换 → 透明调用 → 还原」四步模式即可，关键是不破坏编译正确性与可逆性。
- **阅读建议**：先重读 [docs/tools/lower_trace.md](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/tools/lower_trace.md) 把工作流过一遍，再带着真实问题回到本讲的实践步骤，用你自己的 kernel 跑通一遍。
