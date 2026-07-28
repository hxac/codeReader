# 编译流水线总览：build_program 全流程

## 1. 本讲目标

前面几讲，我们站在「使用者」视角，学会了用 `tilus.Script` 写内核、用 `@autotune` 选调度。从本讲开始，我们要换到「编译器内部」视角：当你在 Python 里调用 `matmul(m, n, k, a, b, c)` 时，Tilus 到底把那段 `__call__` 代码经历了哪些步骤，才变成 GPU 上一个可执行的 `.so`？

本讲学完后，你应当能够：

- 画出从 `tilus.Script` 到 `.so` 共享库的完整编译阶段图，并说出每个阶段的输入和输出。
- 区分 **Tilus IR 优化**（高层、面向张量与布局）与 **Hidet IR 优化**（底层、面向标量与 CUDA）这两层优化的边界与职责。
- 说清楚缓存目录是怎么命名的、缓存键是怎么算出来的，并知道什么时候必须手动清缓存。

本讲是第三单元「Tilus IR 与编译流水线」的入口，后续讲义（Transpiler、IR 结构、IR 工具）会分别钻进本讲描述的某一个阶段内部。

## 2. 前置知识

本讲假设你已经掌握以下概念（来自 U1、U2）：

- **tile-level 编程与张量一等公民**：Tilus 的 `__call__` 描述的是「一个线程块整体做什么」，数据是张量而非标量。
- **Script 与实例化**：`tilus.Script.__new__` 会拦截构造，返回一个已 JIT 编译、可直接调用的 `InstantiatedScript`（见 u2-l1）。
- **自动调优与调度**：`@autotune` 会把搜索空间展开成多份「调度（schedule）」，每份调度对应一份具体的 `Program`，再各自编译（见 u2-l4）。

补充两个本讲要用到、但前面没展开的底层名词：

- **Tilus IR**：Tilus 自己的高层中间表示，以 `Program → Function → Stmt → Instruction/Tensor` 为骨架，张量是一等公民（见 u1-l2 的包结构）。它贴近你写的 `__call__`。
- **Hidet IR**：Tilus 内嵌的 Hidet 项目提供的低层中间表示，已经落到标量、指针、`for` 循环、CUDA launch 的层面。它贴近最终的 CUDA C。
- **Pass（变换/优化遍）**：编译器里对 IR 做一次完整遍历并改写的步骤，比如「死代码消除」「常量折叠」。多个 Pass 串成一条「流水线」。

一句话理解全篇：**`build_program` 就是一条把「张量化的高层 IR」层层「翻译+优化」成「GPU 机器码」的流水线，而缓存让它不必每次都重跑。**

## 3. 本讲源码地图

本讲围绕编译「主编排函数」展开，涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `python/tilus/drivers.py` | **本讲主角**。定义 `build_program`、`optimize_program`、`optimize_ir_module`、`get_cache_dir`、`build_ir_module`，把六大阶段串起来。 |
| `python/tilus/option.py` | 全局选项。提供 `cache_dir`、`debug.dump_ir`、`debug.disable_ptxas_opt` 等，本讲实践会反复用到。 |
| `python/tilus/transforms/__init__.py` | 暴露 `get_default_passes()`，列出高层（Tilus IR）默认 Pass 的顺序。 |
| `python/tilus/transforms/base.py` | Pass 框架：`Pass` 基类、`PassContext`、`apply_transforms`，理解流水线如何被驱动。 |
| `python/tilus/backends/codegen.py` | `generate_ir_module`：把 Tilus IR 翻译（lowering）成 Hidet IR 模块。 |
| `python/tilus/runtime/compiled_program.py` | 加载已编译产物、判断「缓存是否已完成」的依据。 |
| `python/tilus/transforms/instruments/dump_ir.py` | `DumpIRInstrument`：把每个 Pass 之后的 IR 落盘，是本讲实践的核心观察工具。 |
| `examples/matmul/matmul_v0.py` | naive matmul 示例，实践的载体。 |

## 4. 核心概念与源码讲解

### 4.1 build_program 六阶段总览

#### 4.1.1 概念说明

把一个 `Program` 变成可执行的 `.so`，要经过六个阶段。本讲把它们记成一句话：

> **校验 → 高层优化 → 降级 → 底层优化 → 代码生成 → 编译链接。**

这六个阶段可以归成三大块：

- **前置校验**：在动手改之前，先确认程序合法（避免把一个本来就错的程序优化得更难定位）。
- **高层世界（Tilus IR）**：以张量、布局、指令为对象的优化，包括「布局自动推理」「死代码消除」等。这一层离你的 `__call__` 最近。
- **底层世界（Hidet IR → CUDA）**：把张量世界「降级（lowering）」成标量与指针后，再做一轮以标量/循环/CUDA launch 为对象的优化，最后生成 CUDA C 并调用 nvcc 编译。

为什么分两层？因为高层优化（比如「这个寄存器张量该用什么排布」）和张量语义强绑定，必须在张量层面做；而底层优化（比如「循环不变量外提」「子字节类型展开」）只有在落到标量后才有意义。各做各的，互不干扰。

#### 4.1.2 核心流程

下面用伪流程图描述 `build_program` 的整体走向：

```
build_program(prog)
│
├─ get_cache_dir(prog, options)            # 算缓存目录（见 4.3）
│     └─ cache_dir/programs/<12位摘要>/
│
├─ if compiled_program_exists(cache_dir):  # 命中缓存就直接返回，不编译
│     return cache_dir                      ★ 缓存命中（最快路径）
│
└─ with FileLock(cache_dir/.lock):          # 防止多进程重复编译
      ├─ (再查一次缓存)                       # 双重检查
      │
      ├─ 0. verify(prog)                     # 校验合法性
      ├─ 1. prog = optimize_program(...)     # 高层(Tilus IR) Pass 流水线
      ├─ 2. ir_module = generate_ir_module(prog)   # 降级到 Hidet IR
      └─ 3~6. build_ir_module(ir_module, .../module)
            ├─ 4. ir_module = optimize_ir_module(...)  # 底层(Hidet IR) Pass 流水线
            ├─ 5. codegen(...) → source.cu              # 生成 CUDA C
            └─ 6. compile_source(...) → lib.so          # nvcc 编译
```

注意三个关键设计：

1. **缓存优先**：在 `get_cache_dir` 之后立刻判断 `compiled_program_exists`，命中就「零编译」直接返回目录。这就是为什么第二次跑同一个内核几乎是瞬时的。
2. **文件锁**：用 `filelock.FileLock` 串行化同一缓存的编译，防止 autotune 的并行 worker 重复编译同一份程序。
3. **双重检查（double-checked locking）**：拿到锁之后再查一次缓存——因为可能在等锁期间别的进程已经编完了。

#### 4.1.3 源码精读

主编排函数全貌，注意阶段 0/1/2 与 3~6 的分组（注释里直接写明了编号）：

[python/tilus/drivers.py:L280-L325](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/drivers.py#L280-L325) —— `build_program`：先取缓存目录，命中则直接返回；否则加锁后依次执行 校验→高层优化→降级→`build_ir_module`。

阶段 3~6（底层优化、代码生成、编译）被收拢进一个辅助函数：

[python/tilus/drivers.py:L247-L277](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/drivers.py#L247-L277) —— `build_ir_module`：三步走——`optimize_ir_module`（底层优化）→ `codegen` 写 `source.cu` → `compile_source` 产出 `lib.so`。注意它把 `target`（如 `sm80`/`sm90a`）传给编译器，不同架构产出不同机器码。

阶段 0 的校验函数本身很简单——遍历程序收集错误诊断，有错就抛 `VerificationError`：

[python/tilus/ir/tools/verifier.py:L140-L145](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tools/verifier.py#L140-L145) —— `verify`：用 `IRVerifier` 收集 diagnostics，非空则报错。它在任何优化之前运行，是最早的「安全网」。

#### 4.1.4 代码实践

> **实践目标**：用 `dump_ir` 打开 IR 落盘，亲眼看到六大阶段在缓存目录里各自留下的产物。

**操作步骤：**

1. 新建一个脚本（示例代码，非项目原有文件）：

   ```python
   import tilus
   tilus.option.cache_dir("/tmp/tilus-u3l1-cache")   # 指定一个干净的缓存目录
   tilus.option.debug.dump_ir(True)                  # 打开逐 Pass IR 落盘

   import torch, math
   from examples.matmul.matmul_v0 import MatmulV0    # 复用 naive matmul

   m = n = k = 512
   a = (torch.rand(m, k, dtype=torch.float16).cuda() - 0.5) / math.sqrt(k)
   b = (torch.rand(k, n, dtype=torch.float16).cuda() - 0.5) / math.sqrt(k)
   c = torch.empty(m, n, dtype=torch.float16).cuda()

   MatmulV0()(m, n, k, a, b, c)   # 首次调用：触发完整编译
   torch.cuda.synchronize()
   ```

2. 进入缓存目录，找到本次编译对应的 `programs/<12位摘要>/` 子目录（可以按修改时间排序，最新的那个就是）。

3. 列出该目录的树形结构。

**需要观察的现象：**

- 目录里应当出现 `program.txt`、`options.txt`（缓存键的来源，见 4.3）。
- 出现 `ir/` 子目录：里面是 **高层（Tilus IR）** 各 Pass 之后的 IR。
- 出现 `module/` 子目录，里面有 `source.cu`（生成的 CUDA C）、`lib.so`（编译产物），以及 `module/ir/`（**底层（Hidet IR）** 各 Pass 之后的 IR）。

**预期结果：**

- `ir/` 下能看到形如 `0_Original.txt`、`1_DeclareToLet.txt`、… 的文件，编号对应 4.2 里高层 Pass 的执行顺序。
- `module/source.cu` 里是一段真正的 CUDA C kernel。

**待本地验证**：本环境无 GPU，以上命令的实际输出需在你本地具备 NVIDIA GPU 的环境运行确认。`dump_ir` 的精确文件命名见 [4.2.3](#423-源码精读) 的 `DumpIRInstrument`。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `optimize_program`（阶段 1）整段注释掉，程序还能正确编译运行吗？为什么实践中不这么做？

> **参考答案**：能编出 `.so`，但很可能编不出——例如 `lower_load_store` 之前如果没有布局推理，很多张量没有布局，代码生成会失败；即使能编，生成的代码性能与正确性都无保障。阶段 1 是把「用户视角的张量逻辑」准备成「可降级形态」的必经步骤。

**练习 2**：`build_program` 为什么在加锁之后还要「再查一次缓存」？

> **参考答案**：典型的双重检查（double-checked locking）。多个 autotune worker 可能同时编译同一份程序，第一个拿到锁的编完释放锁，后到的在拿到锁后必须重新检查，否则会重复编译浪费时间。

---

### 4.2 两层 IR 优化：Tilus IR Pass 与 Hidet IR Pass

#### 4.2.1 概念说明

「优化」在这条流水线里出现了两次，分别在两个不同的 IR 世界：

| 层次 | 所在阶段 | 操作对象 | 代表性 Pass |
| --- | --- | --- | --- |
| 高层（Tilus IR） | 阶段 1 `optimize_program` | 张量、布局、指令、线程组 | 布局推理 `layout_inference`、死代码消除 `dead_code_elimination` |
| 底层（Hidet IR） | 阶段 4 `optimize_ir_module` | 标量、指针、循环、CUDA launch | 循环不变量外提、子字节类型展开、launch 配置检查 |

两层都遵循同一个 **Pass 框架**：一条 `Pass` 列表，由 `apply_transforms` 依次把每个 Pass 作用到 IR 上。区别只在于「Pass 改的是哪种 IR」。

一个值得记住的细节：**布局推理在高层流水线里跑了两遍**（一次在 `lower_load_store` 前，一次在后）。原因会在 [4.2.3](#423-源码精读) 结合源码解释——`lower_load_store` 会引入新的张量搬运，需要再次为它们推理布局。

#### 4.2.2 核心流程

高层 Pass 的执行框架（来自 `apply_transforms`）：

```
apply_transforms(prog, transforms):
    ctx = PassContext.current()
    ctx.before_all_passes(prog)          # 仪器：开始前（dump_ir 在这里写 0_Original）
    for transform in transforms:
        ctx.before_pass(name, prog)      # 仪器：单个 Pass 前（记开始时间）
        prog = transform(prog)           # ★ 真正执行这个 Pass
        ctx.after_pass(name, prog)       # 仪器：单个 Pass 后（dump_ir 在这里写文件）
    ctx.after_all_passes(prog)           # 仪器：全部结束后（写 lower_time / html）
    return prog
```

这里出现一个新概念：**仪器（Instrument）**。它像「钩子」，在 Pass 执行的前后插入观察逻辑，但不改变 IR 本身。`DumpIRInstrument` 就是一个仪器，靠它把每个 Pass 后的 IR 落盘，这正是我们能在 `ir/` 目录看到中间产物的原因。

底层 Pass 的执行机制类似，只是它跑在 Hidet 的 `PassContext` 里，仪器换成 `SaveIRInstrument`/`ProfileInstrument`，产物落到 `module/ir/`。

#### 4.2.3 源码精读

**（a）高层 Pass 的默认顺序**——`get_default_passes()` 把流水线一五一十列了出来：

[python/tilus/transforms/__init__.py:L31-L45](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/__init__.py#L31-L45) —— 默认 12 个高层 Pass。注意 `layout_inference_pass` 出现了 **两次**（第 7、第 9），夹着第 8 个 `lower_load_store_pass`；`analyze_scalar_pass` 也出现两次（第 5、第 11）。

读这张表的小窍门：前半段（declare→let→assume→param_only→scalar）是「整理 IR 形态」；中段（layout_inference→load_store→layout_inference）是「把布局填满、把搬运落地」；后段（bound_aware_simplify→scalar→dce）是「化简 + 清理」。后续讲义 U5 会逐个精读。

**（b）Pass 框架与仪器挂载点**：

[python/tilus/transforms/base.py:L68-L112](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/base.py#L68-L112) —— `Pass` 基类（`__call__` 转发到 `process_program`，默认逐函数处理）与 `apply_transforms`（按序应用、在前后调用仪器钩子）。

而 `optimize_program` 正是在这里把 `dump_ir` 仪器挂上 `PassContext`：

[python/tilus/drivers.py:L62-L93](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/drivers.py#L62-L93) —— `optimize_program`：取 `get_default_passes()`，若 `debug.dump_ir` 为真则 `ctx.dump_ir(cache_dir / "ir")`（即追加一个 `DumpIRInstrument`），再 `apply_transforms`。注意若设了 `debug_block`，还会额外插入一个 `inject_print_instruction_pass` 用来调试特定线程块。

**（c）仪器如何落盘**——文件命名规则全在这里：

[python/tilus/transforms/instruments/dump_ir.py:L36-L63](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/transforms/instruments/dump_ir.py#L36-L63) —— `DumpIRInstrument`：`before_all_passes` 先清空旧目录并写 `0_Original.txt`；`after_pass` 每跑完一个 Pass 就写 `<序号>_<PassName>.txt`。这就是你能在 `ir/` 看到 `1_DeclareToLet.txt` 这类文件的由来。

**（d）降级与底层优化**：

[python/tilus/backends/codegen.py:L498-L518](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/codegen.py#L498-L518) —— `generate_ir_module`（阶段 2）：`ProgramCodegen` 遍历每个 `Function`，用 `FunctionCodegen` 把 Tilus IR 翻译成 Hidet IR 模块，最后再 `verify_ir_module` 校验一次。

[python/tilus/drivers.py:L96-L189](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/drivers.py#L96-L189) —— `optimize_ir_module`（阶段 4）：一条长长的 Hidet 级 Pass 列表（如 `flatten_tensor_index`、`explicit_unroll`、`hoist_loop_invariants`、`deadcode_elimination`）。若开了 `dump_ir`，则挂 `SaveIRInstrument` 把每步 IR 写到 `module/ir/`，并写一份 `module/ir/lower_time.txt` 记录各 Pass 耗时。

#### 4.2.4 代码实践

> **实践目标**：对比 `ir/`（高层）和 `module/ir/`（底层）的产物数量与风格差异，直观感受「两层 IR」。

**操作步骤：**

1. 沿用 4.1.4 的脚本，编译完成后打开缓存目录。
2. 统计 `ir/` 目录下 `*.txt` 文件数量（应约等于 1 个原始 + 12 个 Pass = 13 个）。
3. 打开 `ir/0_Original.txt` 与 `ir/7_LayoutInference.txt`（若编号有出入，以目录里实际文件名为准），对比张量的 `layout` 字段是否从「空/未定」变成「已填充」。
4. 打开 `module/ir/` 下的任意一个文件，观察它是否已经出现 `threadIdx`、标量循环、CUDA launch 等底层元素。

**需要观察的现象：**

- `ir/` 里的 IR 仍是「张量 + 指令」风格（如 `RegisterTensor(...)`、`DotInst`）。
- `module/ir/` 里的 IR 已经是「标量 + 指针 + for 循环」风格，几乎看不到张量抽象。

**预期结果**：两层 IR 的「抽象高度」明显不同，印证了「高层管张量/布局，底层管标量/循环」的分工。

**待本地验证**：具体 Pass 数量与文件名以本地 `dump_ir` 实际输出为准（编号会随 `get_default_passes()` 变化）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `layout_inference_pass` 要在默认流水线里出现两次？

> **参考答案**：第一次推理为「用户直接写的张量」补齐布局；之后 `lower_load_store_pass` 会把全局↔共享↔寄存器的搬运具象化，引入一批新的中间张量，这些新张量同样需要布局，因此再跑一次 `layout_inference`。简言之：搬运落地会产生「需要重新推理布局」的张量。

**练习 2**：`apply_transforms` 与 `DumpIRInstrument` 是什么关系？仪器能改变 IR 吗？

> **参考答案**：`apply_transforms` 是执行者，`DumpIRInstrument` 是观察者（通过 `PassContext` 挂载）。仪器只在 `before_pass/after_pass` 等钩子里做观察（如落盘、计时），不参与 IR 改写，因此加仪器不会影响编译结果，只影响可观测性。

---

### 4.3 程序哈希缓存机制

#### 4.3.1 概念说明

整条流水线很重（要跑几十个 Pass、生成 CUDA、调 nvcc），所以 Tilus 把「程序文本 + 编译选项」哈希后作为缓存键，编过一次就存下来，下次直接复用 `.so`。理解缓存要抓住三点：

1. **缓存键来自什么**：`str(prog)`（Tilus IR 的文本）加上 `options`（含 `debug_block`、`disable_ptxas_opt`、`target`）的文本。
2. **缓存键不包含什么**：**不包含** codegen/emitter 的输出。也就是说，改了 emitter（比如修了地址计算）不会改变缓存键。
3. **目录结构**：`<cache_dir>/programs/<12位摘要>/`，里面有 `program.txt`、`options.txt`、`module/lib.so` 等。

第 2 点是 Tilus 最容易踩的坑，CLAUDE.md 里专门强调了：

> The cache key is based on the Tilus IR hash, not the codegen output. Changes to the emitter/codegen do NOT invalidate cached programs. You must delete the cache directory to force recompilation after emitter changes.

直白说：**改了 emitter 之后，必须手动删缓存目录**，否则 Tilus 会复用旧的（错的）`.so`，让你的修复看起来「没生效」。

#### 4.3.2 核心流程

缓存目录的解析与「是否命中」的判定：

```
get_cache_dir(prog, options):                  # 被 lru_cache 记忆
    options_dict = asdict(options)
                  + {disable_ptxas_opt, target}
    prog_text    = str(prog)
    options_text = str(options_dict)
    digest = sha256( options_text ‖ prog_text )[0:12]   # 取前 12 位
    cache_dir = <cache_dir>/programs/<digest>/
    # 写入 program.txt / options.txt（若已存在则校验一致）
    return cache_dir

compiled_program_exists(cache_dir):
    return 同时存在 module/lib.so、program.txt、options.txt
```

缓存键的计算公式（`‖` 表示字符串拼接）：

\[
\texttt{digest} \;=\; \mathrm{sha256}\big(\,\texttt{options\_text}\;\Vert\;\texttt{prog\_text}\,\big)\,[\,0:12\,]
\]

为什么取 12 位就够：缓存目录是「按内容命名」的，12 位十六进制有 \(16^{12}\) 种可能，配合「目录已存在则校验文本一致」的兜底（见下文源码），碰撞既能被检测又极罕见。

#### 4.3.3 源码精读

**（a）缓存键计算与目录创建**：

[python/tilus/drivers.py:L192-L244](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/drivers.py#L192-L244) —— `get_cache_dir`：把 `options_dict`（含 `debug_block`、`disable_ptxas_opt`、`target`）与 `prog_text` 拼接做 SHA256 取前 12 位，定位 `programs/<digest>/`。注意两点：① 整个函数被 `@functools.lru_cache(maxsize=1024)` 记忆，同一 `(prog, options)` 不重复算；② 若 `program.txt`/`options.txt` 已存在但内容不符，会直接抛 `ValueError`——这是「同摘要却不同程序」的兜底校验。

**（b）「缓存是否完成」的判定**——只认三件套：

[python/tilus/runtime/compiled_program.py:L65-L82](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/runtime/compiled_program.py#L65-L82) —— `compiled_program_exists`：当且仅当 `module/lib.so`、`program.txt`、`options.txt` 同时存在才算「编完了」。所以一个半途失败的编译（缺 `lib.so`）会被当成未命中、下次重编。

**（c）缓存目录的默认位置与相关选项**：

[python/tilus/option.py:L24-L48](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/option.py#L24-L48) —— `_get_default_cache_dir`：若在 git 仓库内，默认是仓库根的 `.cache/`；否则是 `~/.cache/tilus`。

[python/tilus/option.py:L162-L187](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/option.py#L162-L187) —— `debug.dump_ir` / `debug.disable_ptxas_opt`：两个会影响缓存键的调试开关（`disable_ptxas_opt` 进 `options_dict`；`dump_ir` 本身不进键，只决定是否落盘 IR）。两者都支持同名环境变量 `TILUS_DUMP_IR` / `TILUS_DISABLE_PTXAS_OPT`。

> 关于 CLAUDE.md 里提到的 `/scripts/` 与 `/programs/` 两级结构：`programs/` 是单个 `Program` 编译产物的存放处（本讲的焦点）；`scripts/` 则是 autotune 层面的「脚本模板 + 调优空间」，记录一个 Script 展开出的多份调度与它们对应的 program 引用。本讲的 `build_program` 只直接产生 `programs/` 下的内容。

#### 4.3.4 代码实践

> **实践目标**：验证「同程序命中缓存、改 emitter 不命中」这两条关键性质。

**操作步骤：**

1. 先删掉缓存目录 `rm -rf /tmp/tilus-u3l1-cache`，运行 4.1.4 的脚本，记录首次编译耗时（观察终端，会明显较慢，伴随 nvcc 调用）。
2. 不删缓存，**再次运行同一脚本**（同样的 `m=n=k=512` 与同样的 `block_m/n/k`）。
3. 打开缓存目录里的 `options.txt`，确认里面记录了 `target`、`disable_ptxas_opt` 等字段。

**需要观察的现象：**

- 第二次运行几乎瞬时完成（命中 `compiled_program_exists`，直接返回，不跑任何 Pass、不调 nvcc）。
- 改变任一会影响键的输入——例如把 `__init__` 里的 `block_m` 从 64 改成 128，或换一个 `target`——会产生一个**新的** `<digest>` 子目录并重新编译。

**预期结果**：缓存命中时，`build_program` 在阶段 0 之前就 `return`，根本不进入优化/降级/编译。这正是「第二次跑很快」的原因。

**待本地验证**：实际耗时差异需在本地带 GPU 环境测量。

#### 4.3.5 小练习与答案

**练习 1**：你修了 `python/tilus/backends/emitters/` 里的一个发射器（修了个地址计算 bug），重新运行内核，发现结果没变。最可能的原因是什么？怎么修？

> **参考答案**：因为缓存键只依赖 Tilus IR 文本（`str(prog)`）和 options，**不**依赖 emitter/codegen 输出，所以旧的（带 bug 的）`.so` 仍被命中复用。解决办法：删掉对应的 `programs/<digest>/` 目录（或整个 `.cache`），强制重编译。这正是 CLAUDE.md 强调的「改 emitter 后必须手动清缓存」。

**练习 2**：`get_cache_dir` 用了 `@lru_cache`，而 `compiled_program_exists` 每次都重新读磁盘。为什么一个记忆化、另一个不记忆化？

> **参考答案**：`get_cache_dir` 的输入 `(prog, options)` 在一次进程里是确定且不变的，算出来的目录路径不会变，可以安全记忆化以省去重复哈希。而 `compiled_program_exists` 查询的是「磁盘上 `lib.so` 是否已存在」这个**外部状态**——别的进程可能正在写它，所以必须每次实时查盘，不能记忆化。

## 5. 综合实践

把本讲三块内容串起来，做一个「全链路追踪」小任务：

1. **准备**：`tilus.option.cache_dir("/tmp/tilus-u3l1-trace")` + `tilus.option.debug.dump_ir(True)`，删干净该目录。
2. **首次编译**：运行 `MatmulV0()`（`examples/matmul/matmul_v0.py`）跑一次 512×512×512。
3. **画出产物地图**：在缓存目录里，标注出每个文件/子目录分别属于六大阶段的哪一步。参考下表填写：

   | 文件/目录 | 所属阶段 | 含义 |
   | --- | --- | --- |
   | `program.txt` | 缓存键 | 原始 Tilus IR 文本 |
   | `options.txt` | 缓存键 | 编译选项文本 |
   | `ir/0_Original.txt` | 阶段 1 之前 | 校验后、优化前的 IR |
   | `ir/7_*.txt` | 阶段 1 | 高层某 Pass 后的 IR |
   | `module/ir/*` | 阶段 4 | 底层各 Pass 后的 Hidet IR |
   | `module/source.cu` | 阶段 5 | 生成的 CUDA C |
   | `module/lib.so` | 阶段 6 | nvcc 编译产物 |

4. **验证缓存**：再跑一次同样的内核，确认它走了「命中缓存」的快速路径（不产生新的中间文件、几乎瞬时）。
5. **制造一次「缓存不命中」**：把 `MatmulV0.__init__` 里的 `block_k` 从 16 改成 32，再跑，确认生成了**新的** `<digest>` 目录并重新编译，从而体会「程序文本变了 → 缓存键变了 → 重新编译」。

完成后，你应当能用一张图向别人讲清楚：一段 `__call__` 代码是如何经「校验 → 高层优化 → 降级 → 底层优化 → 代码生成 → 编译」变成 `.so`，又如何被缓存复用的。

## 6. 本讲小结

- `build_program` 是把 `Program` 变成 `.so` 的主编排函数，六大阶段为：**校验 → 高层优化（Tilus IR）→ 降级（生成 Hidet IR）→ 底层优化（Hidet IR）→ 代码生成（CUDA C）→ 编译（nvcc）**。
- 优化分两层：**高层**（`optimize_program` + `get_default_passes()`）面向张量/布局/指令；**底层**（`optimize_ir_module`）面向标量/循环/CUDA launch。两层共用「Pass + Instrument」框架。
- 缓存键 = `sha256(options_text ‖ str(prog))` 取前 12 位，落在 `programs/<digest>/`；**键不包含 codegen 输出**，所以改 emitter 后必须手动删缓存。
- `compiled_program_exists` 以 `module/lib.so` + `program.txt` + `options.txt` 三件套齐备作为「编完」判据，配合 `FileLock` 与双重检查保证多进程下不重复编译。
- `debug.dump_ir` 通过 `DumpIRInstrument`/`SaveIRInstrument` 把每个 Pass 后的 IR 落到 `ir/` 与 `module/ir/`，是观察整条流水线最直接的工具。

## 7. 下一步学习建议

本讲是从「外部」鸟瞰整条流水线。接下来建议按阶段「钻进去」：

- **想看 `__call__` 是怎么变成 Tilus IR 的** → 读 u3-l2《Transpiler：从 Python AST 到 Tilus IR》，它解释阶段 1 之前 Program 是怎么来的。
- **想看清高层 IR 的数据结构** → 读 u3-l3《Tilus IR 结构：Program/Function/Stmt》与 u3-l4《Instruction 与 Tensor》，对应阶段 1 操作的对象。
- **想动手验证 IR** → 读 u3-l5《IR 工具：验证、打印与收集》，本讲的 `verify`/`printer` 就来自这里。
- **想逐个吃透高层 Pass** → 进入 U5《Tilus IR 变换》，本讲列出的 `get_default_passes()` 会被逐个精读。

建议先做本讲的「综合实践」，对缓存目录结构有第一手印象后，再带着具体问题（比如「布局到底是在哪一步被填上的」）进入后续讲义，会顺畅很多。
