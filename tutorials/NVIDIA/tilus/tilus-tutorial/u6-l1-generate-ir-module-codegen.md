# generate_ir_module：Tilus IR → Hidet IR

## 1. 本讲目标

本讲是「后端与代码生成」单元的第一讲。学完之后，你应该能够：

- 说清 `generate_ir_module` 在 `build_program` 六阶段流水线中的位置与职责——它是把**高层 Tilus IR**（`Program/Function/Stmt/Instruction`）**降级**成**底层 Hidet IR**（`IRModule`，贴近 CUDA C）的那一步。
- 跟着 `FunctionCodegen` 走一遍 `visit_Function` 的完整流程：它如何同时建立**设备 kernel 函数**与**主机 launch 函数**两个 `FunctionBuilder`，如何初始化上下文、访问语句树、收尾上下文。
- 理解指令到 CUDA 代码的「两层分派」：语句结构走访问者（`IRFunctor` 的 `visit_*`），而每条指令的具体实现走 **`resolve_inst_emitter` + 发射器注册表（REGISTRY）**。
- 读懂 `launch_kernel` 如何在主机侧生成 `LaunchKernelStmt`，把网格/线程块/共享内存/`extra_params` 串成一次真正的 kernel 启动。
- 掌握「打开缓存里的 `source.cu`，逐段对应回发射器」的调试技能。

本讲承接 u5-l4（lowering 变换）的结尾：那里产出的是**已经 lower 过、布局完备、analysis 就绪**的 `Program`，正好是 `generate_ir_module` 的输入。

## 2. 前置知识

在进入源码前，先用通俗语言对齐几个概念：

- **两层 IR**。Tilus 全程维护两层中间表示。**Tilus IR** 面向张量、布局、指令，是「一个线程块整体做什么」的表达（U3 已讲）。**Hidet IR** 来自内嵌的 hidet 子包，面向标量、循环、CUDA launch，是「一个线程具体执行什么 CUDA C」的表达（`IRModule` 里装的是 `hidet.ir.func.Function`）。`generate_ir_module` 就是这两层之间的翻译器。
- **设备函数 vs 主机函数**。一个 GPU 程序有两类函数：运行在 GPU 上的 `cuda_kernel`（设备侧，被网格里的线程块执行），以及运行在 CPU 上、负责准备参数并 `<<<grid,block>>>` 启动该 kernel 的 `public` 函数（主机侧）。Tilus 的 codegen 会为**同一个 Tilus `Function` 同时生成这两个**。
- **发射器（Emitter）**。Tilus IR 里的一条 `Instruction`（如 `CastInst`、`AddInst`、`LoadGlobalInst`）在 Hidet IR 里没有一一对应的单条语句，而要展开成「给每个线程算地址、发 PTX、插同步」的一串标量语句。完成这种展开的对象就叫发射器，全部继承自 `BaseInstEmitter`。
- **target（编译目标）**。指编译给哪种 GPU，如 `sm80/sm90a/sm100a`。同一条指令在不同架构上往往要用不同的 PTX 指令实现，所以发射器要按 target 区分（U1-l2 已引入）。
- **`FunctionBuilder`**。hidet 提供的 IR 构造器，用 `with builder.if_then(...)` / `builder.declare(...)` / `builder.append(...)` 这样的命令式 API 把语句「追加」进正在构建的函数体。
- **`IRFunctor`**。Tilus 的访问者基类，按节点类型分派到 `visit_<类型名>` 方法（U5-l1 已讲）。`FunctionCodegen` 正是它的子类。

如果你对上面任意一项还陌生，建议先回看 u3-l3（IR 结构）、u3-l4（Instruction/Tensor）、u5-l1（访问者框架）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [python/tilus/drivers.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/drivers.py) | 编译主编排，`build_program` 在这里调用 `generate_ir_module` 完成降级 |
| [python/tilus/backends/codegen.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/codegen.py) | 本讲主角：`generate_ir_module`、`ProgramCodegen`、`FunctionCodegen`、`launch_kernel` |
| [python/tilus/backends/emitter.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitter.py) | 发射器基类 `BaseInstEmitter`、注册表 `REGISTRY`、`@register_emitter` 装饰器 |
| [python/tilus/backends/contexts/contexts.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/contexts/contexts.py) | `EmitContexts`：聚合代码生成期间维护状态的九个上下文 |
| [python/tilus/backends/emitters/cast.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/cast.py) | 一个具体发射器范例（`CastInst`），演示「按 target 选实现」 |
| [python/tilus/target.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/target.py) | `get_current_target` / `match_target`，决定发射器分派结果 |
| [python/tilus/ir/utils/normalize.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/utils/normalize.py) | `normalize_dim3`，把网格/线程块规格化成三元组 |

---

## 4. 核心概念与源码讲解

### 4.1 FunctionCodegen 访问：从 Function 到 IRModule

#### 4.1.1 概念说明

`generate_ir_module` 的真正工作由两类对象完成：

- **`ProgramCodegen`**：外层。一个 `Program` 含若干个 `Function`（绝大多数内核只有一个），它为**每个** `Function` 新建一个 `FunctionCodegen`，各自生成一个子 `IRModule`，再用 `merge_ir_modules` 拼成总模块。
- **`FunctionCodegen`**：内层。它是一个 `IRFunctor`，负责把**一个** Tilus `Function` 翻译成一个 `IRModule`——而这个 `IRModule` 里装着**两个** hidet 函数：一个设备 kernel、一个主机 launch。

理解这一点非常关键：**Tilus 的「一个函数」在 Hidet IR 里裂变成「设备 + 主机」一对函数**，二者通过 `LaunchKernelStmt` 相连。这也是为什么 `FunctionCodegen.__init__` 里同时持有 `_builder`（设备）和 `_host_builder`（主机）。

#### 4.1.2 核心流程

`FunctionCodegen` 处理一个 `Function` 的完整流程（对应 `visit_Function`）：

```text
visit_Function(func):
    1. 校验：func.metadata.analysis 必须非空（由标量分析填充）
    2. 解析 cluster_blocks（仅 sm_90+ 支持 cluster，否则必须为 (1,1,1)）
    3. 建设备 builder：name=func.name+"_kernel"，kind="cuda_kernel"
                     grid_dim=metadata.grid_blocks，block_dim=num_warps*32
    4. 建主机 builder：name=func.name，kind="public"
    5. 两边都 extend_params(func.params)
    6. printer 预热（给张量/变量分配稳定名字）
    7. check_emitter_existence()：预先确认每条指令都有发射器
    8. 初始化 current_thread=threadIdx.x、thread_group_stack、所有 contexts
    9. visit(func.body)：递归访问语句树，逐条「翻译」
    10. contexts.finalize()：收尾（如插入共享内存声明、同步）
    11. 把 extra_params 加到 kernel 参数，结束设备函数
    12. launch_kernel(kernel_function)：在主机侧生成启动语句
    13. 结束主机函数
    14. 打包 IRModule(functions={kernel, host}) 返回
```

注意第 9 步：`visit(func.body)` 是一个**双重分派**过程。一方面，`visit_*` 按语句结构递归（`SeqStmt` 拆开、`IfStmt` 包成 `if_then`、`ForStmt` 包成 `for_loop` 等）；另一方面，遇到 `InstStmt` 时，结构访问到此为止，转交「指令实现」给发射器（见 4.2）。换句话说：

- **语句的「骨架」**（循环、分支、线程组收窄）由 `FunctionCodegen` 自己用 `FunctionBuilder` 复刻；
- **语句叶子上的「指令」**（算术、搬运、同步）外包给发射器。

#### 4.1.3 源码精读

先看入口 `generate_ir_module`：它只是 `ProgramCodegen` 的一层薄包装，外加一次 Hidet IR 校验。

[python/tilus/backends/codegen.py:498-L518](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/codegen.py#L498-L518) —— 这是 `generate_ir_module` 的定义：构造 `ProgramCodegen`、调用得到 `IRModule`，再用 hidet 的 `verify_ir_module` 校验。它**不**做任何优化，优化是后续 `optimize_ir_module` 的事。

[python/tilus/backends/codegen.py:486-L495](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/codegen.py#L486-L495) —— `ProgramCodegen.visit_Program`：逐个函数新建 `FunctionCodegen`，合并各自产出的 `IRModule`。注释点出「最终的 launch 入口由稍后的 `GenerateLaunchFuncPass` 处理」，说明主机 `public` 函数在此还只是中间产物。

再看 `FunctionCodegen` 的状态字段，它们决定了「翻译」如何进行：

[python/tilus/backends/codegen.py:76-L98](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/codegen.py#L76-L98) —— `FunctionCodegen.__init__` 持有的关键状态：
- `_builder` / `_host_builder`：设备 / 主机两个 `FunctionBuilder`；
- `extra_params`：在主机侧计算、要额外传给 kernel 的参数；
- `tensor2var`：Tilus `Tensor` → hidet `Var` 的映射表（发射器通过它读写张量对应的标量变量）；
- `shared_tensor_addr`：`SharedTensor` → 共享内存空间里的 `uint32` 地址；
- `contexts: EmitContexts`：聚合九个维护状态的上下文；
- `thread_group_stack`：维护当前处于哪一段线程（决定 `current_thread` 的取值）。

`visit_Function` 是本节主菜：

[python/tilus/backends/codegen.py:193-L259](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/codegen.py#L193-L259) —— `visit_Function` 全流程，要点：
- L194-195 要求 `metadata.analysis` 非空——这正是流水线里 `analyze_scalar`（标量分析）必须**先于** codegen 运行的原因，发射器会读取 `analysis` 做界感知判断。
- L209-217 建设备 builder：`kind="cuda_kernel"`，`grid_dim=metadata.grid_blocks`，`block_dim=num_warps*32`（一个 warp 32 线程）。`cluster_dim` 仅在 sm_90+ 取 `cluster_blocks`。
- L218-221 建主机 builder：`kind="public"`，名字就是原函数名。
- L232-233 初始化 `current_thread = threadIdx.x`，并压入一个覆盖**全部线程**的根线程组 `ThreadGroupStack`。
- L236 `self.contexts.initialize()`、L242 `self.contexts.finalize()`：上下文有生命周期，`finalize` 往往负责把累积的共享内存声明、同步语句「补」到函数开头或结尾。
- L257 最终 `IRModule` 同时包含 `kernel_function` 与 `host_function`。

语句骨架的复刻在各个 `visit_*` 方法里。举两例：

[python/tilus/backends/codegen.py:276-L284](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/codegen.py#L276-L284) —— `visit_ForStmt`：把 Tilus 的 `unroll_factor`（`None`/`-1`/`n`）映射成 hidet 的循环属性 `.` / `u` / `uN`，最终渲染成 CUDA 的 `#pragma unroll`。这正对应 u2-l3 讲过的 `self.range(unroll=...)` 提示。

[python/tilus/backends/codegen.py:319-L389](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/codegen.py#L319-L389) —— `visit_ThreadGroupStmt`：把「收窄到一段线程执行」翻译成 hidet 的 `if_then(cond)`，并在内部把 `current_thread` 重新定义为该线程组内的局部 `tid`。其中 `thread_begin == -1` 是 u2-l3 讲过的 **elect-any** 模式，`_elect_any_cond`（L286）会针对单线程/单 warp/多 warp 等情况分别挑选 `elect.sync`、`shfl_sync` 或「线程 0」来挑选代表线程，避免线程发散。

#### 4.1.4 代码实践

**实践目标**：把 codegen 产出的 Hidet IR 落到磁盘，亲眼看到「一个 Tilus `Function` 裂变成 kernel + host 两个 hidet 函数」。

**操作步骤**：

1. 确认已安装 Tilus（参考 u1-l2），在脚本最前面打开 IR 落盘：

   ```python
   import tilus
   tilus.option.cache_dir("/tmp/tilus-u6l1-cache")   # 指定一个干净的缓存目录
   tilus.option.debug.dump_ir(True)                  # 落盘每个 Pass 后的 IR
   ```

2. 运行一个最小内核，例如 [examples/vector_add/vector_add.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/vector_add/vector_add.py) 的 `VectorAdd`，用一组 `n`（如 `1 << 20`）触发一次编译。

3. 进入缓存目录 `/tmp/tilus-u6l1-cache/programs/<12位哈希>/`。在 `ir/` 下找到 `generate_ir_module`（或类似命名）阶段后的 IR 文件。

**需要观察的现象**：

- 该 IR 模块里应当有**两个**函数：名字形如 `vector_add_kernel`（`cuda_kernel`）与 `vector_add`（`public`）。
- 设备函数的 `grid_dim` 对应 `cdiv(n, block_elems)`、`block_dim` 等于 `num_warps * 32 = 128`。
- 主机函数里有一条 `launch_kernel(...)` 语句，把指针参数与网格/线程块配置传给设备函数。

**预期结果**：你能指认出哪段是设备 kernel、哪段是主机 launch，并看到二者通过启动语句连接。如果运行环境没有 GPU 或版本不符而无法编译，此步标注为「待本地验证」，但 IR 文件的结构描述仍然成立。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `visit_Function` 在 L194 要求 `metadata.analysis` 不能为 `None`？如果跳过标量分析会怎样？

> 参考答案：发射器在生成代码时常需要变量是否可整除、上下界等信息（例如决定能否用快速除法、是否需要越界掩码），这些信息由 `analyze_scalar` 产出的 `Analysis` 提供（见 u5-l3）。若 analysis 缺失，codegen 直接抛 `RuntimeError`，避免生成错误代码。

**练习 2**：`block_dim` 为什么是 `num_warps * 32` 而不是直接写线程数？

> 参考答案：Tilus 用「warp」作为线程组织的单位，一个 warp 固定 32 线程；`num_warps` 是 `attrs.warps` 设的编译期常量。`*32` 把 warp 数换算成物理线程数，与 CUDA `blockDim` 的语义对齐。

---

### 4.2 resolve_inst_emitter 分派：指令如何找到发射器

#### 4.2.1 概念说明

上一节提到，语句的骨架由 `FunctionCodegen` 自己复刻，而叶子上的指令交给发射器。那么「一条指令 → 哪个发射器类」是如何决定的？

Tilus 采用一个**全局注册表**：`BaseInstEmitter.REGISTRY`，结构是

```text
REGISTRY: { 指令类 -> { target -> 发射器类 } }
```

每条指令类可以按不同 target 注册多个发射器实现（同一指令在 Ampere/Hopper/Blackwell 上可能完全不同）。`resolve_inst_emitter` 的工作就是：给定当前要发射的指令类与当前 target，在注册表里**先匹配指令类（含继承关系）、再匹配最合适的 target**，返回那个发射器类。

这是一个与访问者模式**正交**的第二层分派：`visit_Instruction` 是所有指令的统一入口，真正的「多态」发生在 REGISTRY 里，而不是 `visit_*` 方法里。

#### 4.2.2 核心流程

```text
visit_Instruction(inst):
    1. 插入注释：把 inst 用 IRPrinter 渲染成 /* ... */ 注释（便于在 source.cu 里溯源）
       —— PrintTensorInst / FormatPrintInst 例外，不加注释
    2. emitter_cls = resolve_inst_emitter(inst.__class__)
       若为 None -> RuntimeError（找不到发射器）
    3. emitter = emitter_cls(self)        # 新建发射器实例，注入 codegen
    4. emitter.emit(inst)                 # 发射器把指令展开成一串 hidet 语句
    5. 检查：若 inst.output 非空，必须在 tensor2var 里登记了映射，否则报错
    6. builder.append(emitter.finish())   # 收尾语句（如有）
```

`resolve_inst_emitter` 内部两步：

```text
resolve_inst_emitter(inst_cls):
    target = get_current_target()
    # 第一步：沿继承链找第一个能匹配的注册指令类
    for registry_inst_cls, emitter_classes in REGISTRY.items():
        if issubclass(inst_cls, registry_inst_cls):
            candidates = emitter_classes; break
    # 第二步：在候选 target 里挑「当前 target 支持、且算力最高」的那个
    matched_target = match_target(target, list(candidates.keys()))
    return candidates[matched_target] if matched_target else None
```

`match_target`（[python/tilus/target.py:412-L420](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/target.py#L412-L420)）的挑选规则是：先过滤出当前 target `supports` 的候选，再取**算力（compute_capability）最高**者。这样一条指令如果同时注册了通用的 `nvgpu_any` 与更专门的 `nvgpu_sm90` 发射器，在 Hopper 上会优先用后者。

#### 4.2.3 源码精读

[python/tilus/backends/codegen.py:461-L479](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/codegen.py#L461-L479) —— `visit_Instruction`：注意 L464-465 的注释插入是本讲综合实践的关键——它在生成的 CUDA 里留下 `/* %r0 = CastInst(...) */` 这样的注释，让你能逐行溯源到 Tilus 指令。L468 解析发射器，L472 调 `emit`，L473-478 强制要求「有输出的指令必须登记其输出张量映射」——这是 u3-l4 讲过的「判断产出有无用 `is not None`」在 codegen 里的体现。

[python/tilus/backends/codegen.py:123-L134](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/codegen.py#L123-L134) —— `resolve_inst_emitter`：注意它用 `issubclass(inst_cls, registry_inst_cls)` 匹配，因此子类指令会命中父类注册的发射器；命中后 `break`，说明注册表里**先匹配到的注册指令类**即生效。

[python/tilus/backends/codegen.py:136-L154](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/codegen.py#L136-L154) —— `check_emitter_existence`：在真正发射**之前**做一次预检，遍历函数里所有指令（`collect_instructions`），任何找不到发射器的指令类都收集起来，最后一次性抛出 `CodeGenerationFailed`，并贴心地列出该指令「注册过哪些 target」。这条错误信息是排查「在低架构上用了高架构专属指令」的首要线索。

[python/tilus/backends/emitter.py:34-L36](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitter.py#L34-L36) —— `BaseInstEmitter` 与类级 `REGISTRY` 字典：注册表是挂在基类上的全局状态。

[python/tilus/backends/emitter.py:259-L283](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitter.py#L259-L283) —— `@register_emitter(inst_cls, target=...)` 装饰器：把「指令类 + target」映射到发射器类写进 `REGISTRY`，并禁止重复注册（重复会抛 `ValueError`）。`target` 缺省为 `gpgpu_any`（通用）。

[python/tilus/backends/emitter.py:81-L100](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitter.py#L81-L100) —— `get_or_allocate_var`：发射器把一个 Tilus `Tensor` 映射到 hidet `Var` 的标准入口。对不同张量类型声明不同形态的变量：寄存器张量→`tensor_var`（按 `local_size`）、共享/全局张量→`tensor_pointer_var`、TMEM→`int32`。这张映射表正是 `FunctionCodegen.tensor2var`，被 codegen 与所有发射器共享。

[python/tilus/backends/emitters/cast.py:101-L121](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/cast.py#L101-L121) —— 一个具体例子：`@register_emitter(CastInst, target=nvgpu_any)` 注册 `NvgpuCastInstEmitter`，它在构造时往 `specialized_cast` 字典里塞了一堆 `(源dtype, 目标dtype) -> 专用实现` 的映射（如 `int8→float16` 用 `prmt`+`sub_f16x2`，`int4b→float16` 用 `lop3`）。落到具体发射时（[cast.py:73-L94](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/cast.py#L73-L94)），有专用实现就用专用、否则退回 `cast_generic`（一条标量循环逐元素隐式转换）。这正是「按 target 与 dtype 选 PTX 实现」的活样本。

#### 4.2.4 代码实践

**实践目标**：验证「两层分派」——同一指令类在不同 target 下命中不同发射器。

**操作步骤**：

1. 在 REPL 里 import 后查看注册表：

   ```python
   import tilus  # 触发各 emitters 模块导入，填充 REGISTRY
   from tilus.backends.emitter import BaseInstEmitter
   from tilus.ir.instructions import CastInst
   print(BaseInstEmitter.REGISTRY[CastInst])
   ```

2. 上面会打印 `{<某 target>: NvgpuCastInstEmitter, <另一 target>: AmdgpuCastInstEmitter}`（对应 [cast.py:499-L504](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/cast.py#L499-L504) 的 AMD 注册）。

3. 手动构造一个 `FunctionCodegen` 并调用 `resolve_inst_emitter`：

   ```python
   from tilus.backends.codegen import FunctionCodegen
   fc = FunctionCodegen()
   print(fc.resolve_inst_emitter(CastInst))   # 在 NVIDIA GPU 上应为 NvgpuCastInstEmitter
   ```

**需要观察的现象**：`resolve_inst_emitter` 的返回值与 `match_target` 挑出的 target 一致；在 NVIDIA 设备上返回 `NvgpuCastInstEmitter`。

**预期结果**：注册表里 `CastInst` 对应「按 target 分多份」的字典，且当前 target 命中 NVIDIA 版本。无 GPU 环境下这一步为「待本地验证」，但 `REGISTRY[CastInst]` 的内容可静态确认。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `visit_Instruction` 是统一的，而不为每种指令写一个 `visit_CastInst` / `visit_AddInst`？

> 参考答案：因为「指令具体如何实现」依赖 target，且实现数量庞大、动态注册。把这些塞进访问者会让 `FunctionCodegen` 膨胀并与 target 耦合。用 REGISTRY 做第二层分派，既保持了 codegen 的结构纯粹（只处理语句骨架），又让发射器可独立模块化、按 target 扩展。

**练习 2**：`resolve_inst_emitter` 用 `issubclass` 匹配并 `break`。如果某指令既注册在父类、又注册在自身子类，会命中哪个？

> 参考答案：命中 `REGISTRY` 字典遍历时**第一个**满足 `issubclass(inst_cls, registry_inst_cls)` 的注册项就 `break`。因此更具体的子类注册能否被选中，取决于它在字典里的相对顺序；新增指令若需专属发射器，应直接注册到该指令自身类，并确认其相对顺序优先于父类项。

---

### 4.3 launch_kernel 启动：设备 kernel 与主机 launch 的分离

#### 4.3.1 概念说明

前两节造出了设备 kernel 函数。但 GPU kernel 不会自己运行——必须有 CPU 侧代码调用 `<<<grid, block, smem>>>` 把它启动起来。`launch_kernel` 就是生成这段主机侧启动代码的方法。

它处理三件事：

1. **动态共享内存**：kernel 用了多少共享内存（由 `EmitContexts` 里的 `smem_alloc_ctx` 统计），超过 48KB 时要调用 `cudaFuncSetAttribute` 提升上限；
2. **参数传递**：kernel 参数 = 主机函数收到的指针参数 + `extra_params`（host 侧算好、额外下发的标量）；
3. **启动配置**：从 kernel 函数属性里读 `grid_dim`/`cluster_dim`/`block_dim`，连同共享内存字节数，组装成一条 `LaunchKernelStmt`。

理解 `extra_params` 的存在意义：有些量（如某 workspace 基地址、规约用的全局缓冲指针）在主机侧才能确定，需要作为额外参数传给 kernel。发射器通过 `BaseInstEmitter.append_extra_param` 把一个主机变量登记进来，codegen 在收尾时统一把它们追加到 kernel 参数表。

#### 4.3.2 核心流程

```text
launch_kernel(kernel_func):
    if kernel_func.kind == "cuda_kernel":
        func_var = Var(kernel_func.name, FuncType.from_func(kernel_func))
        dynamic_smem = kernel_func.attrs.dynamic_smem_bytes or 0
        # 越界检查：不得超过设备每块共享内存上限
        if dynamic_smem > 设备上限: raise RuntimeError
        # 超过 48KB 则插入 set_kernel_max_dynamic_smem_bytes
        with host_builder.if_then(dynamic_smem > 48*1024):
            host_builder.append(set_kernel_max_dynamic_smem_bytes(func_var, dynamic_smem))
        # 组装参数：主机参数 + 额外参数
        kernel_args = host_builder.params + extra_params
        cluster_dim = kernel_func.attrs.cluster_dim or 1
        host_builder.append(LaunchKernelStmt(
            func_var, kernel_args,
            grid_dim=normalize_dim3(grid_dim),
            cluster_dim=normalize_dim3(cluster_dim),
            block_dim=normalize_dim3(block_dim),
            shared_mem=dynamic_smem,
            target="cuda"))
    else:
        raise NotImplementedError
```

`normalize_dim3`（[python/tilus/ir/utils/normalize.py:22-L53](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/utils/normalize.py#L22-L53)）把可能是整数、表达式或一/二/三维序列的维度规格化成统一的 `(x, y, z)` 三元组，缺省维补 1。

#### 4.3.3 源码精读

[python/tilus/backends/codegen.py:156-L191](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/codegen.py#L156-L191) —— `launch_kernel` 全文：
- L160-165 取动态共享内存字节数，且要求它是编译期常量（`Constant | int`）——因为 `cudaFuncSetAttribute` 需要具体数值；
- L166-170 与设备 `shared_memory_per_block` 上限比对，超出直接报错；
- L173-174 用 `if_then(dynamic_smem > 48*1024)` 包裹 `set_kernel_max_dynamic_smem_bytes`——48KB 是不调用 `cudaFuncSetAttribute` 时的默认上限，只有超过才需要显式提升；
- L177 `kernel_args = list(self.host_builder.params) + list(self.extra_params)` 是参数拼接的关键一行；
- L178 `cluster_dim` 默认 1（非 cluster 内核）；
- L179-189 构造 `LaunchKernelStmt`，grid/block/cluster 都过 `normalize_dim3`。

`extra_params` 是如何被收集与消费的？看两处呼应：

[python/tilus/backends/emitter.py:229-L236](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitter.py#L229-L236) —— `append_extra_param`：发射器调用它把一个主机变量追加进 `codegen.extra_params`。

[python/tilus/backends/codegen.py:245-L250](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/codegen.py#L245-L250) —— 收尾时 `self.builder.extend_params(self.extra_params)` 把这些额外参数加到**设备 kernel** 的形参表，紧接着 `launch_kernel(kernel_function)` 在主机侧用相同的 `extra_params` 列表作为实参传递。两边列表一致，参数对齐。

最后回到主编排，确认 `generate_ir_module` 在流水线里的确切位置：

[python/tilus/drivers.py:312-L323](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/drivers.py#L312-L323) —— `build_program` 的核心几步：`0. verify` → `1. optimize_program`（Tilus passes）→ `2. generate_ir_module`（本讲）→ `3-6. build_ir_module`（Hidet 优化 + 代码生成 + nvcc）。`generate_ir_module` 产出 `IRModule` 后立即交给 `build_ir_module`。

[python/tilus/drivers.py:247-L277](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/drivers.py#L247-L277) —— `build_ir_module`：`optimize_ir_module`（一大串 hidet passes，如 `generate_launch_func_pass` 才真正把主机 `public` 函数包装成最终 launch 入口、`flatten_tensor_index` 把张量索引拍平、`lower_subbyte_type` 处理 subbyte）→ hidet `codegen` 出 `source.cu` → `compile_source` 走 nvcc 生成 `lib.so`。所以 codegen.py 产出的还只是「半成品 Hidet IR」，要经过这层优化才变成最终 CUDA C。

#### 4.3.4 代码实践

**实践目标**：在最终 `source.cu` 里定位「主机 launch 函数」与「动态共享内存设置」，理解设备/主机分离如何落到真实 CUDA。

**操作步骤**：

1. 沿用 4.1.4 的缓存目录设置（`cache_dir` + `dump_ir`），运行 vector_add 触发编译。
2. 打开 `programs/<哈希>/module/source.cu`（这是 hidet codegen 的最终产物）。
3. 在文件里搜索 `__global__`（设备 kernel）与不含 `__global__`、形如 `void vector_add(...)`（主机 public 函数）。

**需要观察的现象**：

- 主机函数体里能看到等价于 `if (smem > 48*1024) cudaFuncSetAttribute(...)` 的逻辑（vector_add 不用共享内存，所以这段通常不出现或 smem=0，可换一个用共享内存的 matmul 示例观察）。
- 主机函数末尾有一条 `<<<grid, block, smem>>>` 形式的 kernel 启动（hidet 会渲染成对应的 CUDA runtime 启动调用）。
- 设备 kernel 的参数列表 = 主机函数的指针参数（可能再加上额外参数）。

**预期结果**：你能把 `launch_kernel` 生成的三段（动态共享内存设置、参数组装、`LaunchKernelStmt`）一一对应到 `source.cu` 里的 CUDA 语句。无 GPU 环境标注为「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `dynamic_smem` 必须是编译期常量？

> 参考答案：`cudaFuncSetAttribute(..., cudaFuncAttributeMaxDynamicSharedMemorySize, bytes)` 的字节数在 Tilus 里选择在编译期确定（由 `smem_alloc_ctx` 统计得到），这样既能做 L166 的越界静态检查，也能让 `LaunchKernelStmt` 携带确定的 `shared_mem`，便于 hidet 后端直接渲染。若它不是常量，L163 的断言会失败。

**练习 2**：`extra_params` 为什么不在 `func.params` 里，而要单独维护？

> 参考答案：`func.params` 来自用户 `__call__` 签名（指针、运行时标量），是「外部传入」的。`extra_params` 是 codegen **期间**由发射器在主机侧临时算出、需要下发到设备的量（如 workspace 基址）。二者来源不同，故单独收集，最后在设备形参表与主机实参表两处统一拼接，保证对齐。

---

## 5. 综合实践

把三节内容串起来：**在生成的 `source.cu` 里，逐段把 CUDA 代码溯源回 codegen 的某个环节**。

**任务**：

1. 准备环境：

   ```python
   import tilus
   tilus.option.cache_dir("/tmp/tilus-u6l1-cache")
   tilus.option.debug.dump_ir(True)
   ```

2. 用 [examples/vector_add/vector_add.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/vector_add/vector_add.py) 跑一次编译，再打开 `programs/<哈希>/module/source.cu`。

3. 完成下面这张「溯源表」（在 `source.cu` 里找到对应片段，写出它来自哪个 codegen 环节）：

   | `source.cu` 中的内容 | 来自 codegen 的哪个环节 | 依据 |
   | --- | --- | --- |
   | `/* ... = LoadGlobalInst(...) */` 这类注释 | `visit_Instruction` L464-465 插入的指令注释 | 每条指令发射前的注释 |
   | 设备 kernel 里逐线程的地址计算与 `ld.global` | load/store 发射器（经 `resolve_inst_emitter` 分派） | 指令展开 |
   | `for` 循环 + `#pragma unroll` | `visit_ForStmt`（L276-284）把 unroll 提示转属性 | 语句骨架复刻 |
   | 主机函数里的 `<<<...>>>` 启动 | `launch_kernel`（L156-191）的 `LaunchKernelStmt` | 设备/主机分离 |
   | `__global__ void ..._kernel(...)` 的参数表 | `func.params` + `extra_params`（L245, L177） | 参数拼接 |

4. 进阶：换一个使用共享内存的示例（如 [examples/matmul/matmul_v2.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/matmul/matmul_v2.py)），观察 `source.cu` 里是否出现 `extern __shared__` 动态共享内存声明、以及主机侧是否出现共享内存字节数被传给启动配置——这正是 `EmitContexts.smem_alloc_ctx` 统计、`launch_kernel` 透传的结果（`smem_alloc_ctx` 的细节是下一讲 u6-l3 的主题）。

**预期产出**：一张填好的溯源表，证明你已经能在「Tilus 指令 → 发射器 → CUDA 语句」这条链上双向定位。若本地无 GPU，可只读 `ir/` 与 `module/ir/` 下的 IR 文本完成溯源，并在表里标注「待本地验证」编译产物。

## 6. 本讲小结

- `generate_ir_module` 是 `build_program` 的**降级**步骤：把高层 Tilus IR（`Program/Function`）翻译成底层 Hidet IR（`IRModule`），自身**不做优化**，优化交给后续 `optimize_ir_module`。
- `ProgramCodegen` 为每个 `Function` 新建一个 `FunctionCodegen`，**一个 Tilus 函数裂变成「设备 kernel + 主机 launch」两个 hidet 函数**，二者由 `LaunchKernelStmt` 连接。
- `FunctionCodegen` 用**双重分派**：语句骨架（循环/分支/线程组）由自己的 `visit_*` 用 `FunctionBuilder` 复刻；叶子指令由 `resolve_inst_emitter` + `REGISTRY` 找到发射器实现。
- `resolve_inst_emitter` 先按 `issubclass` 匹配指令类，再用 `match_target` 在候选 target 里挑算力最高者；`check_emitter_existence` 在发射前预检，缺发射器时报错并提示已注册的 target。
- `launch_kernel` 负责主机侧启动：动态共享内存上限设置、`host_params + extra_params` 参数拼接、`normalize_dim3` 规格化网格/线程块，最终生成 `LaunchKernelStmt`。
- `visit_Instruction` 在每条指令前插入 `/* 指令文本 */` 注释，是「在 `source.cu` 里溯源回 Tilus 指令」的关键钩子。

## 7. 下一步学习建议

本讲只讲了 codegen 的「调度骨架」——`FunctionCodegen` 如何编排访问、分派、启动。至于**发射器内部如何把张量布局翻译成每线程的标量地址与 PTX**，留给后续几讲：

- **u6-l2 EmitterBase 与发射器注册机制**：精读 `BaseInstEmitter` 的通用能力（`get_or_allocate_var`、`sync`、`single_thread`、`lane_id`/`warp_id` 等）与 `@register_emitter` 的完整用法。
- **u6-l3 EmitContexts：内存分配与同步状态**：展开 `EmitContexts` 聚合的九个上下文（`smem_alloc_ctx`/`sync_ctx`/`gmem_alloc_ctx` 等），理解共享内存分配、同步插入、leader lane 优化的来龙去脉。
- **u6-l4 通用发射器**：精读 elementwise/reduce/ldst/shared_ldst 等发射器如何逐线程展开运算。

建议带着本讲的「溯源表」继续：每学一个发射器/上下文，就回 `source.cu` 里找它对应的 CUDA 片段，形成闭环理解。
