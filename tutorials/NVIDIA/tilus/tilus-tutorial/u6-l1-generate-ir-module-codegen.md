# generate_ir_module：Tilus IR → Hidet IR

## 1. 本讲目标

本讲是「后端与代码生成」单元的第一篇，承接 u5-l4 讲完的 lowering 变换——那些变换把高层 Tilus IR 准备成了「可以落地」的形态。现在我们要回答一个核心问题：

**`Function.body` 里那棵由 `InstStmt`/`ForStmt`/`ThreadGroupStmt` 组成的 Tilus IR 树，是怎么变成 CUDA C 源码的？**

具体学完后你应当能够：

- 说清 `FunctionCodegen` 如何遍历一个 `Function`，并把「设备 kernel 函数」和「主机 launch 函数」分别建出来。
- 解释 `resolve_inst_emitter` 如何用一张「指令类 → {目标架构 → 发射器}」的全局注册表，把每条 `Instruction` 分派到正确的发射器。
- 描述 `launch_kernel` 如何在主机侧生成 `LaunchKernelStmt`，把网格、线程块、cluster、共享内存字节等启动配置拼起来。
- 在缓存目录里读 `source.cu`，并对照 `codegen` 逻辑反推每段 CUDA 来自哪个发射器。

本讲只讲**「主编排」**：`codegen.py` 这层是如何调度访问、如何分派、如何启动的。至于每个发射器内部如何把张量布局翻译成逐线程标量地址，那是 u6-l2（发射器注册机制）、u6-l3（EmitContexts）、u6-l4（通用发射器）的主题。

## 2. 前置知识

阅读本讲前，请确认你已经掌握以下概念（来自前置讲义）：

- **Tilus IR 树**（u3-l3）：`Function(name, params, body, metadata)`，`body` 是 `Stmt` 树；叶子几乎都是 `InstStmt`，它包着一条 `Instruction`。还有 `ThreadGroupStmt` 收窄执行线程、`ForStmt`/`IfStmt` 等控制流。
- **Instruction / Tensor**（u3-l4）：每条指令有 `output / inputs / attributes` 三段；四种 Tensor（Register/Shared/Global/TMemory）用**身份相等**（`is`，非 `==`）。
- **Metadata**（u3-l3）：携带 `grid_blocks`、`cluster_blocks`、`num_warps`、`analysis` 等编译信息。
- **编译流水线六阶段**（u3-l1）：`build_program` 里 `verify → optimize_program(Tilus passes) → generate_ir_module(Hidet IR) → optimize_ir_module → codegen(CUDA C) → nvcc`。本讲聚焦第三阶段 `generate_ir_module`。
- **Hidet IR**（u3-l1）：Tilus 传承自 Hidet 的低层 IR，贴近 CUDA C，由 `FunctionBuilder`、`Var`、`LaunchKernelStmt` 等构成。

本讲引入的关键术语：

- **设备函数（device/kernel function）**：真正跑在 GPU 上的 `__global__` 函数，Tilus 里用 `FunctionBuilder(kind="cuda_kernel")` 建。
- **主机函数（host/launch function）**：跑在 CPU 上、负责「准备参数 + 启动 kernel」的 `public` 函数，由 `launch_kernel` 生成 `LaunchKernelStmt`。
- **发射器（Emitter）**：负责把**一条** `Instruction` 翻译成若干 Hidet IR 语句的对象，是 `codegen` 的「工人」。
- **REGISTRY**：`BaseInstEmitter` 类上的全局字典，记录「哪种指令、在哪个 target 上、用哪个发射器」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [python/tilus/backends/codegen.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/codegen.py) | 本讲主角。`FunctionCodegen` 遍历 Function、分派指令、生成设备+主机双函数；`ProgramCodegen` 遍历 Program；`generate_ir_module` 是对外入口。 |
| [python/tilus/drivers.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/drivers.py) | `build_program` 编排六阶段；第 2 阶段调用 `generate_ir_module`，第 3-6 阶段在 `build_ir_module` 里做 Hidet IR 优化、codegen、nvcc 编译。 |
| [python/tilus/backends/emitter.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitter.py) | `BaseInstEmitter` 基类、`REGISTRY` 注册表、`@register_emitter` 装饰器、发射器通用能力（`get_or_allocate_var`、`sync` 等）。是分派机制的另一半。 |
| [python/tilus/backends/contexts/contexts.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/contexts/contexts.py) | `EmitContexts` 持有 9 个发射期上下文（共享内存分配、同步、barrier 等），有 `initialize/finalize` 生命周期。 |
| [python/tilus/ir/func.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/func.py) | `Function`/`Metadata` 定义，提供本讲要用到的 `grid_blocks`、`cluster_blocks`、`num_warps`、`analysis` 等字段。 |
| [python/tilus/backends/emitters/cast.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/cast.py) | `CastInst` 发射器实例，用来演示一个发射器如何落地指令。 |

## 4. 核心概念与源码讲解

### 4.1 设备/主机双函数模型与 FunctionCodegen 的访问骨架

#### 4.1.1 概念说明

一条 Tilus IR 的 `Function` 描述的是「**一个线程块要做什么**」——它的 `body` 是以线程块视角书写的逻辑（比如「加载一个 tile、做矩阵乘、写回」）。但真正放到 GPU 上跑，需要两段截然不同的代码：

1. **设备侧 kernel**：一段 `__global__` 函数，里面是每个线程实际执行的标量运算。`grid_blocks` 个线程块、每块 `num_warps × 32` 个线程都会跑这同一段代码。
2. **主机侧 launch**：一段 CPU 函数，负责把指针参数原样传给 kernel、算出需要的额外参数（如动态共享内存字节数），然后发起一次 `<<<grid, block, smem>>>` 启动。

`FunctionCodegen` 的核心职责，就是**用同一次遍历，同时填好这两个函数**。它继承 `IRFunctor`（访问者模式，见 u5-l1），用 `visit_*` 方法把 Tilus IR 树「翻译」进**设备 builder**；而**主机 builder** 主要由 `launch_kernel` 在结尾统一填。

为什么要把 kernel 名字和 host 名字分开？看构造：设备函数名为 `func.name + "_kernel"`，主机函数名沿用 `func.name`。对外暴露的是主机函数，它内部再 `LaunchKernelStmt` 调用那个 `_kernel`。这样调用方只看到一个普通函数，启动细节被封装。

#### 4.1.2 核心流程

`FunctionCodegen` 遍历一个 Function 的全过程（对应 `visit_Function`）：

```text
visit_Function(func):
  0. 前置检查：metadata.analysis 必须非空（标量分析是 codegen 的硬依赖）
  1. 解析 cluster：sm_90+ 支持 cluster_blocks，否则必须是 (1,1,1)
  2. 建两个 FunctionBuilder：
        device builder  : name=func.name+"_kernel", kind="cuda_kernel",
                          grid_dim=metadata.grid_blocks,
                          block_dim=metadata.num_warps * 32
        host builder    : name=func.name, kind="public"
  3. 两个 builder 都 extend_params(func.params)      # 参数对齐
  4. warmup printer (self.printer(func))             # 给张量/变量预分配可读名字
  5. check_emitter_existence()                       # 预检：所有指令都有发射器吗？
  6. 初始化线程视角：current_thread = threadIdx.x
     thread_group_stack.push(0, num_warps*32)        # 整个线程块
  7. contexts.initialize()                           # 9 个发射期上下文就绪
  8. self.visit(func.body)                           # 真正遍历语句树 → 填设备 builder
  9. contexts.finalize()                             # 收尾（如分配 barrier、收尾同步）
 10. builder.extend_params(extra_params) + finish_func → kernel_function
 11. launch_kernel(kernel_function)                  # 填主机 builder，生成 LaunchKernelStmt
 12. host_builder.finish_func → host_function
 13. 返回 IRModule(functions={kernel_function, host_function})
```

设备函数的线程块大小有一个贯穿全项目的恒等式：

\[ \text{block\_dim} = \text{num\_warps} \times 32 \]

即每块线程数 = warp 数 × 32（一个 warp 32 线程）。这个值在 `__init__` 里由 `attrs.warps`（编译期常量，见 u1-l3）确定，最终落到 `metadata.num_warps`，这里被原样取出。

#### 4.1.3 源码精读

`FunctionCodegen.__init__` 建立所有「翻译」要用的状态：

[python/tilus/backends/codegen.py:77-98](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/codegen.py#L77-L98) — 持有两个 builder（`_builder` 设备、`_host_builder` 主机）、`extra_params`（主机算好再传给设备的参数）、`tensor2var`（Tilus 张量 → Hidet 变量的映射，是贯穿发射器的核心字典）、`shared_tensor_addr`（共享张量在 shared space 的 uint32 地址）、`contexts`（9 个发射期上下文）以及线程视角 `_current_thread` 和 `thread_group_stack`。

`visit_Function` 的主体——双 builder 构造与遍历编排：

[python/tilus/backends/codegen.py:193-259](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/codegen.py#L193-L259) — 注意第 194-195 行：**`analysis` 是 codegen 的硬前提**（标量分析的整除性/上下界被发射器用来生成更优地址，所以 lowering 流水线必须先跑 `analyze_scalar`）。第 209-221 行建两个 builder，第 222-223 行二者共享 `func.params`。第 232-233 行把线程视角初始化为 `threadIdx.x`、整个线程块入栈。第 239 行 `self.visit(func.body)` 才是真正的遍历入口。第 250 行 `launch_kernel` 在设备函数建完后填主机函数。最后第 257 行把两个函数装进一个 `IRModule` 返回。

控制流语句的访问很直接，以 `visit_ForStmt` 为例：

[python/tilus/backends/codegen.py:276-284](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/codegen.py#L276-L284) — 它把 `ForStmt.unroll_factor` 翻译成 Hidet builder 的 `attr`：`None` → `"."`（默认）、`-1` → `"u"`（全展开）、`n` → `"u{n}"`（展开 n 次）。这正是 u2-l3 讲过的 `self.range(unroll=...)` 提示落地的地方。

`ThreadGroupStmt` 的访问稍复杂，因为它要把「一段代码收窄给部分线程」翻译成 Hidet 的 `if` 谓词：

[python/tilus/backends/codegen.py:319-389](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/codegen.py#L319-L389) — 它根据 `thread_begin` 是否为 `-1`（elect-any 模式）以及是否 warp 对齐，计算一个执行条件 `cond` 和局部线程号 `tid_value`（见 `_elect_any_cond`，286-317 行用 `elect.sync`/`shfl_sync` 产生 warp 一致的谓词），然后用 `if_then(cond)` 包住 body，并在 body 内把 `current_thread` 换成新的 `tid`。这就是 u2-l3「线程组」在 codegen 层的最终形态：**线程组 = 一段带线程谓词的 Hidet 代码块**。

最后是 `ProgramCodegen` 和对外入口 `generate_ir_module`：

[python/tilus/backends/codegen.py:482-518](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/codegen.py#L482-L518) — `ProgramCodegen.visit_Program` 对 Program 里每个 Function 各起一个 `FunctionCodegen`，把各自的 `IRModule` 合并；`generate_ir_module` 是 `build_program` 第 2 阶段调用的函数，它在生成后还调用 `verify_ir_module` 校验 Hidet IR 合法性。

它在驱动里的位置：

[python/tilus/drivers.py:318-323](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/drivers.py#L318-L323) — `build_program` 第 2 步 `ir_module = generate_ir_module(prog)`，随后 `build_ir_module` 接着做 Hidet IR 优化、CUDA codegen、nvcc 编译（247-277 行）。

#### 4.1.4 代码实践

**实践目标**：用 `dump_ir` 观察一个真实内核被 `FunctionCodegen` 翻译后的 Hidet IR，确认「设备 kernel + 主机 launch」双函数确实被建出来。

**操作步骤**：

1. 准备一个最小工作脚本（示例代码，基于 `examples/vector_add`）：

   ```python
   # demo_codegen.py （示例代码）
   import tilus
   from tilus import float32, int32
   from tilus.utils import cdiv

   tilus.option.cache_dir("/tmp/tilus-codegen-demo")  # 指定缓存目录便于观察
   tilus.option.debug.dump_ir()                        # 开启逐阶段 IR 落盘

   class VectorAdd(tilus.Script):
       def __init__(self):
           super().__init__()
           self.block_elems = 1024
       def __call__(self, n: int32, a_ptr: ~float32, b_ptr: ~float32, c_ptr: ~float32):
           self.attrs.blocks = (cdiv(n, self.block_elems),)
           self.attrs.warps = 4
           offset = self.block_elems * self.blockIdx.x
           ga = self.global_view(a_ptr, dtype=float32, shape=[n])
           gb = self.global_view(b_ptr, dtype=float32, shape=[n])
           gc = self.global_view(c_ptr, dtype=float32, shape=[n])
           ra = self.load_global(ga, offsets=[offset], shape=[self.block_elems])
           rb = self.load_global(gb, offsets=[offset], shape=[self.block_elems])
           self.store_global(gc, ra + rb, offsets=[offset])

   import torch
   n = 1 << 20
   a = torch.randn(n, device="cuda"); b = torch.randn(n, device="cuda"); c = torch.empty(n, device="cuda")
   VectorAdd()(n, a, b, c)
   ```

2. 运行 `python demo_codegen.py`（若无 GPU，至少会编译到缓存阶段；若驱动不支持，标注「待本地验证」）。

3. 进入缓存目录 `/tmp/tilus-codegen-demo/programs/<12位哈希>/module/ir/`。

**需要观察的现象**：

- `module/ir/` 下应有多个 `.txt` 文件，对应 `optimize_ir_module` 里每个 Hidet Pass 之后的 IR（见 u3-l1）。
- 打开最早的几个文件，应能看到名为 `VectorAdd_kernel`（设备，`kind=cuda_kernel`）和 `VectorAdd`（主机，`kind=public`）两个函数。
- 设备函数里应能看到形如 `threadIdx.x`、`blockIdx.x` 的预定义变量和一段逐元素循环。

**预期结果**：双函数模型可见，`_kernel` 后缀清晰区分设备/主机。如果某台机器没有 GPU，这一步需要标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `visit_Function` 第一步要检查 `metadata.analysis is not None`？如果不检查会怎样？

**参考答案**：因为发射器（如地址计算、循环边界化简）依赖标量分析给出的整除性与上下界。`analysis` 由 `analyze_scalar` Pass 产出（u5-l3）。若为 `None`，发射器拿到空的界信息可能生成次优甚至错误的地址表达式，因此 codegen 把它设为硬前提并主动报错。

**练习 2**：设备函数和主机函数的参数列表是否完全相同？

**参考答案**：基本相同——二者都 `extend_params(func.params)`（222-223 行）。但设备函数在结尾还会 `extend_params(self.extra_params)`（245 行），追加那些「主机算好、设备要用」的额外参数（如某些 workspace 偏移），而主机函数在 `launch_kernel` 里把 `host_builder.params + extra_params` 一起传给 kernel（177 行），保证二者签名对齐。

---

### 4.2 指令到发射器的分派：resolve_inst_emitter 与 REGISTRY

#### 4.2.1 概念说明

遍历到 `InstStmt` 时，`FunctionCodegen` 自己并不知道 `CastInst`、`MmaDotInst`、`StoreGlobalInst` 各该怎么翻译——这些知识分散在几十个**发射器**里。`codegen` 的设计是「**主编排只管调度，具体翻译交给发射器**」：

- 发射器是一个 `BaseInstEmitter` 子类，实现 `emit(inst)` 方法，负责把一条指令翻译成若干 Hidet IR 语句。
- 每个发射器在导入时用 `@register_emitter(指令类, target=架构)` 把自己注册进全局表 `BaseInstEmitter.REGISTRY`。
- `FunctionCodegen.visit_Instruction` 遍历到指令时，调用 `resolve_inst_emitter` 查表，找到当前 target 下最合适的发射器，实例化它，调用 `emit`。

这套「注册表 + 按架构匹配」的设计带来两个好处：一是新增一条指令只需写一个发射器并注册，**完全不用改 `codegen.py`**；二是同一指令在不同架构（如 Ampere 的 `mma` vs Hopper 的 `wgmma`）可以用不同发射器，由 target 自动选择。

#### 4.2.2 核心流程

分派的两层结构：

```text
# 注册侧（模块导入时执行一次）
@register_emitter(CastInst, target=nvgpu_any)
class NvgpuCastInstEmitter(BaseInstEmitter):
    def emit(self, inst): ...
# → REGISTRY[CastInst] = { nvgpu_any: NvgpuCastInstEmitter, amdgpu_any: AmdgpuCastInstEmitter }

# 查询侧（遍历到每条指令时）
resolve_inst_emitter(inst_cls):
  for registry_inst_cls, emitter_classes in REGISTRY.items():
      if issubclass(inst_cls, registry_inst_cls):     # 找到匹配的注册指令类
          matched_target = match_target(target, list(emitter_classes))  # 选最强匹配架构
          return emitter_classes[matched_target]      # 返回发射器类
  return None
```

`REGISTRY` 的结构是**嵌套字典**：

\[ \text{REGISTRY}: \text{Instruction 类} \;\mapsto\; \big(\text{Target} \;\mapsto\; \text{Emitter 类}\big) \]

`match_target` 的选择规则（target.py:412-420）：在所有「当前 target 支持」的候选架构里，挑**计算能力最高**的那个。例如一台 sm_90（Hopper）机器，若某指令同时注册了 `nvgpu_any`（任意 NVIDIA）和 `nvgpu_sm90`（Hopper 专属），会优先选 `nvgpu_sm90` 的发射器——这就是 Hopper 用 `wgmma`、Blackwell 用 `tcgen05` 的自动切换原理。

查询命中后，`visit_Instruction` 还会做一件对**调试极其重要**的事：在 emit 之前插一条 `/* ... */` 注释，内容是该指令的 IRPrinter 文本。这条注释会原样出现在最终 `source.cu` 里，让你能从 CUDA 源码反推回 Tilus 指令（4.3 的实践就靠它）。

#### 4.2.3 源码精读

`REGISTRY` 定义在 `BaseInstEmitter` 类上：

[python/tilus/backends/emitter.py:34-36](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitter.py#L34-L36) — `REGISTRY: Dict[Type[Instruction], Dict[Target, Type["BaseInstEmitter"]]]`，即上述嵌套字典。

`@register_emitter` 装饰器负责把发射器登记进表：

[python/tilus/backends/emitter.py:259-283](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitter.py#L259-L283) — `target` 缺省为 `gpgpu_any`（任意 GPU）；若同一 `(指令类, target)` 已注册会直接抛错（防止重复注册掩盖 bug）；否则写入 `REGISTRY[inst_cls][target] = emitter_cls`。

`FunctionCodegen.resolve_inst_emitter` 是查询入口：

[python/tilus/backends/codegen.py:123-134](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/codegen.py#L123-L134) — 遍历 REGISTRY，用 `issubclass(inst_cls, registry_inst_cls)` 找到首个匹配（这支持「为基类注册一个发射器，所有子类都用它」，如 `ElementwiseBinaryBaseInst` 的发射器覆盖所有二元运算子类）；找到后用 `match_target` 选架构，返回对应发射器类。

预检 `check_emitter_existence` 在遍历前一次性确认所有指令都有发射器：

[python/tilus/backends/codegen.py:136-154](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/codegen.py#L136-L154) — 它用 `collect_instructions` 收集 Function 里所有指令（u3-l5），对每个查 `resolve_inst_emitter`，若返回 `None` 就记下，最后一次性抛 `CodeGenerationFailed`，并列出缺失发射器的指令及其已注册的 target，提示你「这条指令需要 sm_90 才能编译」。这避免了「编译到一半才在某条指令上崩」的糟糕体验。

真正遍历到指令时的入口 `visit_InstStmt` → `visit_Instruction`：

[python/tilus/backends/codegen.py:458-479](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/codegen.py#L458-L479) — 第 463-465 行插入 IRPrinter 注释（跳过 `PrintTensorInst`/`FormatPrintInst` 避免噪声）；第 468-472 行解析发射器并调用 `emitter.emit(inst)`；第 473-478 行做健全性检查——**若指令有 output 但发射器没在 `tensor2var` 里登记它，就报错**（防止发射器忘记给产出张量分配 Hidet 变量，导致下游指令找不到它）；第 479 行 `emitter.finish()` 把发射器缓存的语句 flush 进设备 builder。

发射器如何「登记产出」？看 `get_or_allocate_var`：

[python/tilus/backends/emitter.py:81-100](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitter.py#L81-L100) — 这是发射器把 Tilus 张量映射成 Hidet 变量的标准入口：寄存器张量→`tensor_var`（一维，长度为 `local_size`）、共享张量→`tensor_pointer_var`（指向共享内存）、全局张量→`tensor_pointer_var`（指向显存）、TMEM 张量→一个 `int32` 句柄。第一次访问时分配并写入 `tensor2var`，之后命中缓存。这个 `tensor2var` 正是 `FunctionCodegen.__init__` 里那个字典（经 `tensor2var` property 透传给发射器）。

一个最简发射器实例——一元元素级运算：

[python/tilus/backends/emitters/elementwise.py:36-43](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/elementwise.py#L36-L43) — `ElementwiseUnaryInstEmitter.emit`：读输入张量的 Hidet 变量、为输出张量 `get_or_allocate_var`，然后开一个 `for_range(local_size)` 循环，逐元素 `inst.f_compute(v)` 写回。这就是 `ra + rb` 这类运算（u1-l3）最终落地成的 Hidet 循环。注意它用 `@register_emitter(ElementwiseUnaryBaseInst)` 注册到基类，于是 `AddInst`/`MulInst` 等所有子类都自动复用它。

一个更典型的「按架构特化」实例——`CastInst` 发射器：

[python/tilus/backends/emitters/cast.py:101-121](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/cast.py#L101-L121) — `NvgpuCastInstEmitter` 用 `@register_emitter(CastInst, target=nvgpu_any)` 注册；它的 `specialized_cast` 字典为 `(src_dtype, dst_dtype)` 组合挂上用 PTX 位操作（`prmt`/`lop3`/`sub_f16x2`）实现的高速转换，否则回退到 `cast_generic` 的逐元素隐式转换（96-98 行）。同一文件 499-503 行还有 `AmdgpuCastInstEmitter`——这就是「同指令、不同 target、不同发射器」的活样本。

#### 4.2.4 代码实践

**实践目标**：亲手体验「新增一条指令只需注册发射器、无需改 codegen」的扩展性，并验证 target 匹配。

**操作步骤**（源码阅读型实践，不修改项目源码）：

1. 在 `python/tilus/backends/emitters/` 下用 Grep 统计有多少个 `@register_emitter` 调用，确认发射器是分散注册的：

   ```bash
   grep -rn "@register_emitter" python/tilus/backends/emitters/ | wc -l
   ```

2. 选一条你熟悉的指令（如 `CastInst`），找到它在两个 target（`nvgpu_any` / `amdgpu_any`）下的两个发射器类（cast.py:101 与 cast.py:499）。

3. 对照 `match_target`（target.py:412-420）推断：在一台 sm_90 的 NVIDIA GPU 上，`resolve_inst_emitter(CastInst)` 会返回哪个发射器类？

**需要观察的现象**：

- 发射器数量众多且分布在多个文件，每个文件聚焦一类指令。
- `CastInst` 在 REGISTRY 里对应 `{nvgpu_any: NvgpuCastInstEmitter, amdgpu_any: AmdgpuCastInstEmitter}`。

**预期结果**：在 NVIDIA GPU 上，`match_target` 过滤出 `nvgpu_any`（因为 sm_90 supports nvgpu_any），返回 `NvgpuCastInstEmitter`；AMD 上则返回 `AmdgpuCastInstEmitter`。这验证了「同指令按架构自动选发射器」。

#### 4.2.5 小练习与答案

**练习 1**：如果一条新指令 `FooInst` 忘了写发射器，编译时会在哪一步、以什么形式报错？

**参考答案**：在 `visit_Function` 的第 229 行 `check_emitter_existence()` 就会报错——它遍历所有指令预检，发现 `resolve_inst_emitter(FooInst)` 返回 `None`，抛出 `CodeGenerationFailed`，并列出 `FooInst (no registered emitters)` 或其已注册的 target 列表。因此报错发生在真正 emit 之前，且信息清晰。

**练习 2**：为什么 `resolve_inst_emitter` 用 `issubclass(inst_cls, registry_inst_cls)` 而不是精确相等匹配？

**参考答案**：为了支持「为指令基类注册一个发射器、所有子类自动复用」。例如 `ElementwiseBinaryBaseInst` 的发射器覆盖 `AddInst`、`MulInst` 等几十个子类，无需为每个子类重复注册。首个匹配即返回（break），所以更具体的基类应避免与更通用的基类产生歧义。

**练习 3**：`visit_Instruction` 第 473-478 行的检查「output 未登记就报错」防止了什么 bug？

**参考答案**：防止发射器忘了给产出张量调用 `get_or_allocate_var`/写入 `tensor2var`。若漏掉，下游任何消费该 output 的指令在查 `tensor2var` 时会 `KeyError`，错误信息远离根因。这个前置检查把错误锚定到「哪个发射器没登记产出」，便于定位。

---

### 4.3 主机侧启动：launch_kernel

#### 4.3.1 概念说明

设备函数建好后，还差「谁来启动它」。在 CUDA 里，启动一个 kernel 需要指定：函数指针、参数列表、`gridDim`（线程块网格）、`blockDim`（每块线程数）、`clusterDim`（Hopper 起的线程块聚类）、动态共享内存字节数。在 Hidet IR 里，这一切被封装成一条 `LaunchKernelStmt`。

`launch_kernel` 就是把这些信息组装成这条语句、填进主机 builder 的函数。它只处理 `cuda_kernel`（HIP kernel 当前会 `NotImplementedError`），核心做三件事：

1. 取出设备函数的 `dynamic_smem_bytes` 属性，校验它不超过设备每块共享内存上限。
2. 若动态共享内存 > 48KB，生成一条 `set_kernel_max_dynamic_smem_bytes` 调用（CUDA 默认每块 48KB 共享内存，超过需显式申请）。
3. 生成 `LaunchKernelStmt`，把主机参数 + 额外参数一起传给设备函数。

#### 4.3.2 核心流程

```text
launch_kernel(kernel_func):
  if kernel_func.kind == "cuda_kernel":
      func_var = Var(kernel_func.name, FuncType.from_func(kernel_func))   # 设备函数指针
      dyn_smem = kernel_func.attrs.dynamic_smem_bytes or 0
      assert dyn_smem 是常量整数
      if dyn_smem > device.shared_memory_per_block:  raise RuntimeError   # 超限直接拒
      if dyn_smem > 48KB:
          host_builder.append(set_kernel_max_dynamic_smem_bytes(...))     # 申请超额共享内存
      kernel_args = host_builder.params + extra_params                    # 主机参数 + 额外参数
      cluster_dim = kernel_func.attrs.cluster_dim or 1
      host_builder.append(LaunchKernelStmt(
          func_var, kernel_args,
          grid_dim   = kernel_func.attrs.grid_dim,        # = metadata.grid_blocks
          cluster_dim= cluster_dim,
          block_dim  = kernel_func.attrs.block_dim,       # = num_warps * 32
          shared_mem = dyn_smem,
          target     = "cuda"))
  else:
      raise NotImplementedError
```

注意 `grid_dim`/`cluster_dim`/`block_dim` 来自设备函数的属性，而这些属性正是 `visit_Function` 建 builder 时从 `metadata`（`grid_blocks`、`cluster_blocks`、`num_warps×32`）填进去的——所以**启动配置的信息源头是 `Script.__call__` 里的 `attrs.blocks`/`attrs.warps`**（见 u1-l3），一路经 metadata → builder attrs → LaunchKernelStmt 流到最终的 `<<<grid, block, smem>>>`。

#### 4.3.3 源码精读

`launch_kernel` 全貌：

[python/tilus/backends/codegen.py:156-191](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/codegen.py#L156-L191) — 第 158 行只处理 `cuda_kernel`。第 160-165 行取 `dynamic_smem_bytes`（由 `SharedMemoryAllocationContext` 在发射期累计，见 u6-l3）并断言它是编译期常量整数（动态共享内存大小必须在 codegen 时确定）。第 166-170 行做超限校验。第 173-174 行：超过 48KB 时插入 `set_kernel_max_dynamic_smem_bytes`。第 177 行把 `host_builder.params`（即 `func.params`）和 `extra_params` 拼成完整 kernel 参数。第 178 行取 `cluster_dim`（无则为 1）。第 179-189 行生成 `LaunchKernelStmt`，三个维度都经 `normalize_dim3` 规整成三元组。

`cluster` 在 `visit_Function` 开头的处理：

[python/tilus/backends/codegen.py:198-206](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/codegen.py#L198-L206) — sm_90+ 才允许 `cluster_blocks` 非 `(1,1,1)`，否则报错。这保证 cluster 只在支持的架构上启用，与 u2-l2 讲的 `cluster` 指令组（Hopper+）一致。

主机函数的最终装配在 `visit_Function` 末尾：

[python/tilus/backends/codegen.py:249-259](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/codegen.py#L249-L259) — 先 `launch_kernel(kernel_function)` 填主机 builder，再 `host_builder.finish_func()` 收尾，最后把 kernel 与 host 两个函数装进 `IRModule`。运行时（u8-l3）加载 `.so` 后，对外暴露的就是这个主机函数，调用它即触发 `LaunchKernelStmt`。

#### 4.3.4 代码实践

**实践目标**：在最终生成的 `source.cu` 里找到主机 launch 函数，定位 `LaunchKernelStmt` 落地成的 CUDA 启动宏，并核对 grid/block/cluster/smem 配置。

**操作步骤**：

1. 复用 4.1.4 的脚本（或直接用 `examples/vector_add/vector_add.py`），设好 `tilus.option.cache_dir("/tmp/tilus-codegen-demo")` 后运行一次，确保编译产物落盘。
2. 打开 `/tmp/tilus-codegen-demo/programs/<12位哈希>/module/source.cu`（路径来自 drivers.py:269 `output_path / "source.cu"`，其中 `output_path = cache_dir / "module"`）。
3. 在 `source.cu 里搜索 launch 调用（通常是形如 `VectorAdd<<<...>>>` 或通过启动包装函数的调用），以及设备函数 `VectorAdd_kernel` 的定义。

**需要观察的现象**：

- `source.cu` 里有一个 `__global__ void VectorAdd_kernel(...)` 设备函数和一个主机侧的 `void VectorAdd(...)` 包装函数。
- 主机函数体内应有启动 kernel 的语句，其 grid 维度对应 `cdiv(n, 1024)`、block 维度对应 `4 warps × 32 = 128` 个线程、cluster 为 1。
- 设备函数体内，每条 Tilus 指令对应的位置前应有 `/* ... */` 注释（由 visit_Instruction 第 465 行插入），注释文本是该指令的 IRPrinter 输出。

**预期结果**：你能从 `source.cu` 里读出完整的「主机 launch → 设备 kernel」结构，并通过 `/* */` 注释把每段 CUDA 代码反推回具体的 Tilus 指令与发射器。例如某段逐元素相加的循环上方会有类似 `/* Add(...) */` 的注释，对应 `ElementwiseBinaryInstEmitter`。若运行环境无 GPU，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么动态共享内存超过 48KB 时要额外调用 `set_kernel_max_dynamic_smem_bytes`？

**参考答案**：CUDA 硬件每块默认最多 48KB 动态共享内存（opt-in 到更大需要 `cudaFuncSetAttribute`）。超过这个默认值就必须显式声明申请，否则 launch 会失败。`launch_kernel` 用 `if_then(dyn_smem > 48*1024)` 条件生成这条调用，只在真正需要时才插入（173-174 行）。

**练习 2**：`LaunchKernelStmt` 的 `block_dim` 从哪里来？追踪到用户代码是哪一行？

**参考答案**：`block_dim = kernel_func.attrs.block_dim`，而设备函数的 `block_dim` 在 `visit_Function` 第 214 行被设为 `func.metadata.num_warps * 32`；`num_warps` 来自 `metadata`，其源头是用户在 `__call__` 里写的 `self.attrs.warps = 4`（如 vector_add.py:35）。所以一条用户赋值语句最终决定了 kernel 启动时的每块线程数 128。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次「**从 Tilus 指令到 CUDA 源码的全程溯源**」。

**任务**：选择 `examples/vector_add/vector_add.py`（或 `examples/matmul/matmul_v0.py`，若你想看到更丰富的指令），完成下列溯源：

1. **设缓存 + 开 dump**：在脚本开头加 `tilus.option.cache_dir("/tmp/tilus-trace")` 和 `tilus.option.debug.dump_ir()`，运行一次。

2. **找双函数**：打开 `/tmp/tilus-trace/programs/<hash>/module/source.cu`，确认存在 `<Name>_kernel`（设备）和 `<Name>`（主机）两个函数。对应 4.1 讲的双函数模型。

3. **定位 launch**：在主机函数里找到 kernel 启动语句，记录它的 `grid_dim` / `block_dim` / `shared_mem`，并反推：grid 来自 `attrs.blocks`（经 `cdiv`）、block 来自 `attrs.warps × 32`、smem 来自发射期累计。对应 4.3。

4. **溯源指令**：在设备函数里挑两处带 `/* */` 注释的代码段（如 load、add、store），根据注释里的指令名（如 `Load`、`Add`、`Store`），到 `python/tilus/backends/emitters/` 下找到对应的发射器文件（如 `ldst.py`、`elementwise.py`），阅读它的 `emit` 方法，解释这段 CUDA 是怎么由发射器逐线程展开生成的。对应 4.2。

5. **画一张时序图**：画出从 `kernel(n, a, b, c)` 调用到 GPU 执行的完整链路——`build_program` 第 2 阶段 `generate_ir_module` → `FunctionCodegen.visit_Function` → 遍历 body 分派到各发射器 → `launch_kernel` → Hidet IR 优化 → CUDA codegen → nvcc → `LaunchKernelStmt` 启动。

**验收标准**：你能指着 `source.cu` 的任意一段说清「它来自哪个 Tilus 指令、由哪个发射器生成、主机侧如何启动它」。

## 6. 本讲小结

- `FunctionCodegen` 用一次访问同时构建**设备 kernel 函数**（`name_kernel`，`kind=cuda_kernel`，`block_dim=num_warps×32`）和**主机 launch 函数**（`name`，`kind=public`），二者共享 `func.params`，装进一个 `IRModule` 返回。
- `visit_Function` 严格要求 `metadata.analysis` 非空；先建双 builder、`check_emitter_existence` 预检、初始化线程视角与 9 个 `EmitContexts`，再遍历 `body`，最后 `launch_kernel` 填主机函数。
- 指令翻译走「**注册表 + 按架构匹配**」：发射器用 `@register_emitter(指令类, target)` 注册进 `BaseInstEmitter.REGISTRY`（嵌套字典 `指令类 → {Target → 发射器}`）；`resolve_inst_emitter` 用 `issubclass` 匹配基类、`match_target` 选计算能力最高的架构。
- `visit_Instruction` 在 emit 前插入 IRPrinter 注释（落在 `source.cu` 里），emit 后检查产出张量已登记 `tensor2var`，这是从 CUDA 源码反推 Tilus 指令的关键线索。
- `launch_kernel` 把 `grid_dim/cluster_dim/block_dim/shared_mem` 组装成 `LaunchKernelStmt`，超 48KB 共享内存时自动插入 `set_kernel_max_dynamic_smem_bytes`；启动配置的信息源头是用户代码里的 `attrs.blocks` / `attrs.warps`。
- 整个 `codegen.py` 只管**调度**，具体翻译能力由分散在 `backends/emitters/` 的发射器提供，新增指令无需改动 `codegen.py`。

## 7. 下一步学习建议

本讲只讲了「主编排」。要真正理解一段 CUDA 是怎么逐线程生成的，建议接着学：

- **u6-l2 EmitterBase 与发射器注册机制**：深入 `BaseInstEmitter` 的通用能力（`get_or_allocate_var`、`sync`、`single_thread`、`current_thread` 等属性），理解发射器如何拿到线程视角与张量映射。
- **u6-l3 EmitContexts：内存分配与同步状态**：精读 `SharedMemoryAllocationContext`（动态共享内存怎么累计成 `dynamic_smem_bytes`）、`SyncContext`（命名 barrier 怎么分配）、`LeaderLaneContext` 等，它们解释了 `launch_kernel` 里 smem 字节数与同步语句的来源。
- **u6-l4 通用发射器**：逐个精读 `elementwise`/`reduce`/`ldst`/`shared_ldst` 发射器，看它们如何把 `RegisterLayout` 翻译成每线程的标量地址与运算。
- 想看「主编排」在驱动里如何与前后阶段衔接，可回头读 [python/tilus/drivers.py:280-325](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/drivers.py#L280-L325) 的 `build_program`，把本讲放进六阶段流水线的全局图景。
