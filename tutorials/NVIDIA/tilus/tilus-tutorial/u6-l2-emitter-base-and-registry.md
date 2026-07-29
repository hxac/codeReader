# EmitterBase 与发射器注册机制

> 本讲是「后端与代码生成」单元的第二讲（u6-l2），承接 u6-l1 讲过的 `generate_ir_module` 调度骨架，下钻到指令如何被「发射器（emitter）」翻译成 Hidet IR。建议先读 u6-l1，确认你理解 `FunctionCodegen` 的双重分派与 `visit_Instruction` 这个入口。

## 1. 本讲目标

学完本讲，你应该能够：

- 说出 `BaseInstEmitter` 是什么、它为所有发射器提供了哪些「通用能力」（变量分配、同步、线程组查询、语句构造）。
- 解释全局注册表 `REGISTRY` 的数据结构，以及 `@register_emitter(inst_cls, target)` 装饰器如何把「一条指令 + 一个 target」绑定到一个发射器类。
- 描述「按 target 匹配发射器」的完整流程：`resolve_inst_emitter` 用 `issubclass` 找指令、`match_target` 用算力最高者优先挑 target。
- 读懂 `CastInst` 这条真实指令的发射器，说明它如何根据 `(src_dtype, dst_dtype)` 在「标量通用 cast」与「PTX 向量化特殊 cast（prmt/lop3/f16x2）」之间做选择。

## 2. 前置知识

本讲默认你已经掌握以下概念（若陌生，请先回看对应讲义）：

- **两层 IR 与降级**（u6-l1）：Tilus IR（`Program/Function/Stmt/Instruction/Tensor`）经 `generate_ir_module` 翻译成贴近 CUDA C 的 Hidet IR；发射器就是这个翻译过程里「负责某一条指令」的最小单元。
- **Instruction 的三段结构**（u3-l4）：每条指令有 `output / inputs / attributes`，发射器要读 inputs、写 output。
- **Tensor 的身份相等**（u3-l4）：Tensor 用 `is` 判等，`tensor2var` 这个 dict 以 Tensor 身份为键。
- **target**（u1-l2）：编译目标，如 `nvgpu_sm80 / nvgpu_sm90a / nvgpu_sm100a`，由 `get_current_target()` 给出。
- **ThreadGroupStmt / 线程组**（u2-l3、u3-l3）：一段代码可能只由部分线程执行，发射器需要知道「当前有多少线程在跑」。

一个直觉性的比喻：如果把 `FunctionCodegen` 比作一个「翻译公司的调度台」，那么 `REGISTRY` 就是「员工花名册」，`@register_emitter` 是「入职登记」，`resolve_inst_emitter` 是「按任务（指令）和资质（target）派单」，而 `BaseInstEmitter` 是所有员工共享的「办公工具箱」（声明变量、插入同步、查线程号……）。每个员工（具体发射器）只要实现一个核心方法：`emit(inst)`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [python/tilus/backends/emitter.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitter.py) | 定义 `BaseInstEmitter` 基类（含 `REGISTRY`、通用能力）与 `register_emitter` 装饰器。本讲的核心文件。 |
| [python/tilus/backends/codegen.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/codegen.py) | `FunctionCodegen` 在此。本讲用到它的 `resolve_inst_emitter`（派单）与 `visit_Instruction`（调用入口）。 |
| [python/tilus/target.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/target.py) | `Target` 定义、`Target.supports`（兼容性判定）与 `match_target`（挑最优 target）。 |
| [python/tilus/backends/emitters/elementwise.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/elementwise.py) | 最简单的发射器范例：`ElementwiseUnaryInstEmitter` / `ElementwiseBinaryInstEmitter`。 |
| [python/tilus/backends/emitters/cast.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/cast.py) | `CastInst` 发射器范例：演示「同一条指令、不同 target 注册不同发射器」与「按 dtype 分派 PTX 指令」。 |

## 4. 核心概念与源码讲解

### 4.1 BaseInstEmitter：发射器基类与通用 emit 能力

#### 4.1.1 概念说明

一条 Tilus 指令（如 `AddInst`、`CastInst`、`LoadGlobalGenericInst`）最终要变成一段 Hidet IR 语句（循环、标量运算、内建函数调用）。**发射器（emitter）**就是「把某一条指令翻译成一段 Hidet IR」的对象，它的核心方法是 `emit(inst)`。

但绝大多数发射器都需要做很多**重复的杂事**：

- 把一个 Tilus `Tensor` 映射成一个 Hidet `Var`（寄存器/共享/全局张量各对应不同类型的变量）。
- 在合适的位置插入线程同步（`__syncthreads`、mbarrier 等）。
- 查询「我现在在哪个线程组、有多少线程、我是第几号线程」。
- 用 `FunctionBuilder` 的各种糖（`for_range`、`if_then`、`buffer_store`、`declare_var`）拼装语句。

`BaseInstEmitter` 就是把这些杂事集中起来的**基类**。它继承自 hidet 的 `StmtBuilder`（语句构造器），所以天生就会 `append / declare / for_range / buffer_store / if_then` 这些拼语句的能力；再额外加上 Tilus 特有的「张量↔变量映射」「同步」「线程组」三层工具。具体发射器只需继承它并实现 `emit`。

#### 4.1.2 核心流程

一个发射器被调用的典型流程（与 u6-l1 的 `visit_Instruction` 对齐）：

1. `FunctionCodegen.visit_Instruction` 拿到一条 `inst`，通过 `resolve_inst_emitter` 查出对应的发射器**类**。
2. 实例化：`emitter = emitter_cls(self)`——把 `FunctionCodegen` 自己传进去，发射器由此拿到 builder、contexts、tensor2var 等一切上下文。
3. 调用 `emitter.emit(inst)`：发射器在里面读 `inst.inputs`、写 `inst.output`，用基类工具拼出一串 Hidet 语句。
4. 收尾：`FunctionCodegen` 调 `emitter.finish()` 拿到拼好的语句块，挂回函数体。

```text
inst  ──▶ resolve_inst_emitter ──▶ EmitterCls(self) ──▶ emit(inst) ──▶ finish() ──▶ Hidet IR 语句
                                   (持有 codegen)        (用基类工具)     (StmtBuilder 收尾)
```

#### 4.1.3 源码精读

`BaseInstEmitter` 的定义与那个贯穿全讲的全局注册表 `REGISTRY`：

[python/tilus/backends/emitter.py:34-44](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitter.py#L34-L44) 定义基类、`REGISTRY` 类属性与构造函数。注意 `REGISTRY` 是**类属性**（挂在 `BaseInstEmitter` 上），全局唯一；构造时把外层 `FunctionCodegen` 存为 `self._codegen`，这是发射器访问一切上下文的入口。

最关键的通用能力之一是 `get_or_allocate_var`——「张量→Hidet 变量」的惰性映射：

[python/tilus/backends/emitter.py:81-100](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitter.py#L81-L100) 说明：第一次遇到一个 `Tensor` 时，按其内存空间声明对应变量并登记到 `tensor2var`（寄存器张量→`tensor_var`、共享/全局张量→`tensor_pointer_var`、TMEM→普通 `int32` 变量）；之后再遇到同一个张量直接复用。这个 `tensor2var` dict 实际住在 `FunctionCodegen` 上（见 `tensor2var` property），因此**同一条指令的输入变量天然能在前驱指令里找到**——这是发射器之间传递数据的隐式通道。

同步与线程组查询：

[python/tilus/backends/emitter.py:62-65](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitter.py#L62-L65) `sync()` 把同步的复杂性委托给 `contexts.sync_ctx`（u6-l3 详讲）：如果当前上下文需要插入一条同步语句就 `append`，否则什么都不做。发射器只管喊一声「这里要同步」，具体插 `__syncthreads` 还是 mbarrier 由同步上下文决定。

[python/tilus/backends/emitter.py:137-151](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitter.py#L137-L151) 一组只读 property，从 `self._codegen.thread_group_stack` 读出「当前线程组有多少线程、从第几号开始」。配合 `assert_is_single_thread / assert_is_a_warp / assert_is_warp_aligned`（[第 46-60 行](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitter.py#L46-L60)），发射器能在运行前自检线程配置是否满足硬件指令的前提（例如 `ldmatrix` 要求恰好 32 线程且 warp 对齐）。

抽象入口：

[python/tilus/backends/emitter.py:255-256](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitter.py#L255-L256) `emit` 在基类里直接 `raise NotImplementedError`——每个具体发射器必须覆盖它，这是契约。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：确认「发射器的所有能力都来自 `BaseInstEmitter` + `StmtBuilder`」，并找出 `tensor2var` 的真实归属。
2. **步骤**：
   - 打开 [python/tilus/backends/emitter.py:173-175](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitter.py#L173-L175)，看到 `tensor2var` 其实 `return self._codegen.tensor2var`。
   - 再到 [python/tilus/backends/codegen.py:90](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/codegen.py#L90) 确认 `self.tensor2var: Dict[Tensor, Var] = {}` 定义在 `FunctionCodegen.__init__`。
   - 浏览 `BaseInstEmitter` 的 `@property` 列表（builder / contexts / host_builder / kernel_params / analysis / num_warps …），数一数有多少个其实是「转发到 `self._codegen`」。
3. **观察现象**：几乎所有上下文都是转发，发射器自己几乎不持有状态。
4. **预期结果**：你会得出结论——发射器是**无状态的小翻译器**，所有共享状态集中在 `FunctionCodegen`，发射器之间通过 `tensor2var` 等 dict 隐式传递数据。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `get_or_allocate_var` 对寄存器张量用 `tensor_var`、对共享/全局张量用 `tensor_pointer_var`？

> **答案**：寄存器张量是每个线程**私有**的一块数据，直接声明成一维 `tensor_var`（值语义）即可，每个线程各自持有一份；共享/全局张量是**一块被多线程共享的内存**，发射器需要拿到它的**基地址指针**再按布局算偏移，所以声明成 `tensor_pointer_var`（指针语义），用 `ptr[i]` 写入。

**练习 2**：`emit` 在基类里只抛 `NotImplementedError`，这相当于哪种设计模式？

> **答案**：模板方法/抽象方法。基类规定「所有发射器都必须实现 `emit`」的契约，由 `FunctionCodegen.visit_Instruction` 统一调用，子类负责具体翻译。

---

### 4.2 REGISTRY 与 @register_emitter：注册表与装饰器

#### 4.2.1 概念说明

有了基类，还差一个机制回答：「给定一条指令 `inst`，该用哪个发射器类？」Tilus 用一个**全局注册表** `REGISTRY` 来回答。它的结构是两层嵌套字典：

```python
REGISTRY: Dict[Type[Instruction], Dict[Target, Type[BaseInstEmitter]]]
#           键1: 指令类          键2: target   值:   发射器类
```

也就是说，注册表按「指令类」分桶，每个桶里再按「target」细分到具体发射器。这样一个 `(指令, target)` 二元组唯一确定一个发射器类。

往注册表里登记的动作由装饰器 `@register_emitter(inst_cls, target=...)` 完成——它贴在某个发射器类的定义上方，类被定义的瞬间（即模块被 import 时）就自动登记。这种「装饰器即注册」是 Python 里很常见的插件式架构。

#### 4.2.2 核心流程

`@register_emitter(CastInst, target=nvgpu_any)` 装饰一个类时发生的事：

1. `register_emitter` 工厂收到 `inst_cls=CastInst`；若未给 `target`，默认用 `gpgpu_any`（最通用、任何 GPU 都支持）。
2. 返回一个 `decorator(emitter_cls)`。
3. Python 在定义完被装饰的类后调用 `decorator(NvgpuCastInstEmitter)`：
   - 若 `REGISTRY` 里还没有 `CastInst` 这个桶，先建空桶 `{}`。
   - **重复登记检查**：若 `(CastInst, nvgpu_any)` 已有发射器，直接 `raise ValueError`（带上已登记与新登记两个模块名，方便定位冲突）。
   - 否则写入 `REGISTRY[CastInst][nvgpu_any] = NvgpuCastInstEmitter`。
4. 返回原类（装饰器不改类本身，只做登记副作用）。

```text
@register_emitter(CastInst, target=nvgpu_any)
class NvgpuCastInstEmitter(...): ...   #  ← 类定义语句执行完
        │
        ▼  decorator(NvgpuCastInstEmitter)
REGISTRY[CastInst][nvgpu_any] = NvgpuCastInstEmitter   # 副作用：登记
```

两个重要推论：

- **注册是 import 的副作用**：只有被 `import` 到的模块里的装饰器才会执行。`emitters/__init__.py` 的 import 列表决定了哪些发射器真正进入 `REGISTRY`（参见 [python/tilus/backends/emitters/__init__.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/__init__.py)，它 `from . import (cast, elementwise, cuda, ...)` 把所有发射器模块拉进来）。
- **同一指令可挂多个 target**：例如 `CastInst` 在 [cast.py:101](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/cast.py#L101) 挂 `nvgpu_any`、在 [cast.py:499](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/cast.py#L499) 挂 `amdgpu_any`，两个完全不同的发射器类——这就是「跨厂商后端」的实现方式。

#### 4.2.3 源码精读

`register_emitter` 的完整实现：

[python/tilus/backends/emitter.py:259-283](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitter.py#L259-L283) 说明：工厂函数接收 `inst_cls` 与可选 `target`（缺省 `gpgpu_any`）；`decorator` 内部先建桶、再做重复登记检查（第 272-278 行抛出带模块名的 `ValueError`）、最后写入并返回原类。

「同指令多 target」的活样本——`CastInst` 在 NVIDIA 与 AMD 上各注册一个发射器：

[python/tilus/backends/emitters/cast.py:101-102](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/cast.py#L101-L102) `@register_emitter(CastInst, target=nvgpu_any)` + `class NvgpuCastInstEmitter`：所有 NVIDIA GPU 走这个，里面塞满了 PTX 向量化技巧。

[python/tilus/backends/emitters/cast.py:499-503](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/cast.py#L499-L503) `@register_emitter(CastInst, target=amdgpu_any)` + `class AmdgpuCastInstEmitter`：AMD GPU 走这个，`specialized_cast` 为空（只回退到通用实现）。

「多指令共享一个发射器」的写法——装饰器可以叠放：

[python/tilus/backends/emitters/ldst.py:93-94](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/ldst.py#L93-L94) 两个装饰器叠在同一个类上，把 `LoadGlobalGenericInst` 与 `StoreGlobalGenericInst` 都登记到同一个发射器（target 缺省为 `gpgpu_any`）。

「按基类注册，覆盖所有子类」的写法：

[python/tilus/backends/emitters/elementwise.py:22-23](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/elementwise.py#L22-L23) 注册在 `ElementwiseUnaryBaseInst`（**基类**）上，于是 `SqrtInst`、`ExpInst` 等所有一元元素wise子类都自动用同一个发射器——这是靠 `resolve_inst_emitter` 里的 `issubclass` 实现的（见 4.3）。

#### 4.2.4 代码实践（源码阅读型）

1. **目标**：体会「注册是 import 副作用 + 重复登记会报错」。
2. **步骤**：
   - 在仓库根目录运行 `python -c "import tilus.backends.emitters; from tilus.backends.emitter import BaseInstEmitter; import pprint; pprint.pprint({k.__name__: {t.kind+'/'+t.arch: v.__name__ for t,v in d.items()} for k,d in BaseInstEmitter.REGISTRY.items()})"`。
3. **观察现象**：打印出一张「指令 → {target → 发射器类}」的大表；能看到 `CastInst` 同时有 `nvgpu/any` 与 `amdgpu/any` 两个条目，`ElementwiseUnaryBaseInst` 只有一条但覆盖所有子类。
4. **预期结果**：表的键数量 = 实际被 import 的发射器模块里所有 `@register_emitter` 的去重数量；这就是当前进程「派单」的完整依据。若环境无 GPU 导致某些模块未导入，条目会相应缺失。
5. 若上述命令在你的环境因缺 GPU 依赖而失败，属正常，标注「待本地验证」即可——你也可以改成只 `import tilus.backends.emitters.cast` 后再打印 `REGISTRY` 里 `CastInst` 的桶，观察两个 target 条目。

#### 4.2.5 小练习与答案

**练习 1**：如果有人写了两个发射器，都用 `@register_emitter(FooInst, target=nvgpu_sm80)`，会发生什么？什么时候发生？

> **答案**：当第二个被装饰的类**所在模块被 import** 时，`register_emitter` 的重复检查（第 272-278 行）会抛 `ValueError`，并列出先、后两个发射器所在的模块名。即「冲突在 import 期就暴露」，而不是等到编译某条内核时才出错。

**练习 2**：为什么 `ElementwiseUnaryBaseInst` 的发射器不需要对每个子类（`SqrtInst`、`ExpInst`…）各写一个 `@register_emitter`？

> **答案**：因为它注册在**基类**上，而 `resolve_inst_emitter` 用 `issubclass(子类, 基类)` 判定归属——任何 `ElementwiseUnaryBaseInst` 的子类都能命中这唯一一条登记。子类只需各自提供 `f_compute`（具体运算），发射器统一调用它即可（见 4.4.3 的 elementwise 源码）。

---

### 4.3 按 target 匹配发射器：resolve_inst_emitter + match_target

#### 4.3.1 概念说明

注册表是「指令 → {target → 发射器}」。当 `FunctionCodegen` 拿到一条具体的 `inst` 时，要分两步定出发射器：

1. **定指令桶**：`inst` 的类可能并未直接注册（比如它是某个基类的子类），所以要遍历 `REGISTRY` 的键，用 `issubclass` 找到匹配的桶。
2. **定 target**：一个桶里可能有多个 target（如 `gpgpu_any`、`nvgpu_any`、`nvgpu_sm80`、`nvgpu_sm90a`），而当前编译目标只有一个。要在「当前 target 支持的所有候选」里挑**算力最高（最特化）的那一个**。

第二步由 `match_target` 完成，它的判据是 `Target.supports`：「当前 target 能否运行候选 target 要求的程序」。例如当前是 `nvgpu_sm90a`，它能 `supports(nvgpu_sm80)`（高算力向下兼容），也能 `supports(gpgpu_any)`、`supports(nvgpu_any)`，于是三者都入选，`max` 按算力挑出 `nvgpu_sm80`（在没更特化候选时）。这样既保证可用、又尽量用最贴近硬件的实现。

#### 4.3.2 核心流程

```text
inst_cls  ──┐
            ▼
  for key in REGISTRY:                 # 遍历每个已注册指令类
      if issubclass(inst_cls, key):    # 子类也算命中
          candidates = REGISTRY[key]   # {target: emitter}
          break                        # 只取第一个命中的桶
            ▼
  matched = match_target(current_target, candidates.keys())
            │
            │  1) supported = [t for t in candidates if current_target.supports(t)]
            │  2) return max(supported, key=算力)   # 最特化优先；空则 None
            ▼
  emitter_cls = candidates[matched]
```

两个要点：

- `break`：只取**第一个** `issubclass` 命中的桶。由于 Python dict 保序（插入序＝import 序），若同一指令既注册了基类又注册了更具体的子类，**先被 import 的那条登记会胜出**。实践中 Tilus 统一在合适的粒度（通常是基类）注册，以避免歧义。
- 最特化优先：`match_target` 用 `(major, minor)` 取 `max`，确保能用 `nvgpu_sm80` 专用发射器时就不用更通用的 `gpgpu_any` 发射器，从而拿到更高效的实现。

#### 4.3.3 源码精读

`resolve_inst_emitter`（派单逻辑，住在 `FunctionCodegen`）：

[python/tilus/backends/codegen.py:123-134](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/codegen.py#L123-L134) 说明：取当前 target；遍历 `REGISTRY`，用 `issubclass(inst_cls, registry_inst_cls)` 找到桶并 `break`；再调 `match_target` 在桶里挑 target；命中则返回对应发射器类，否则返回 `None`。

`match_target`（挑最优 target，住在 `target.py`）：

[python/tilus/target.py:412-420](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/target.py#L412-L420) 说明：先用 `target.supports(tt)` 过滤出当前 target 能跑的候选模板，再用 `max(..., key=算力)` 取算力最高者；一个都没有就返回 `None`。

`Target.supports`（兼容性判定，含 NVIDIA 后缀语义）：

[python/tilus/target.py:43-81](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/target.py#L43-L81) 说明：`gpgpu_any` 永远被支持；kind 不同（NVIDIA vs AMD）直接不支持；同属 nvgpu 时按 `(major, minor)` 与 `feature_suffix`（`None`/`f`/`a`）判定。`a`（架构专属，如 `sm90a`/`sm100a`）最严：必须同代同版且后缀也是 `a`——这解释了为何 wgmma/tcgen05 这类「架构专属指令」必须用 `sm90a`/`sm100a` target 才能编译。

派单失败的兜底——`check_emitter_existence`：

[python/tilus/backends/codegen.py:136-154](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/codegen.py#L136-L154) 说明：在真正发射前，先把函数里所有指令过一遍 `resolve_inst_emitter`，对任何「找不到发射器」的指令汇总成清晰错误（列出指令名与已注册的 target），抛 `CodeGenerationFailed`。这样「缺发射器」会在编译早期、带着友好信息暴露，而不是在某条指令上含糊地崩。

调用入口——`visit_Instruction`（u6-l1 已讲骨架，这里聚焦与发射器的契约）：

[python/tilus/backends/codegen.py:461-479](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/codegen.py#L461-L479) 说明：先插一条注释（把指令可读文本写进 CUDA，方便对着 `source.cu` 溪源）；解析并实例化发射器、调 `emit`；**关键校验**——若 `inst.output is not None` 但发射器没把它登记进 `tensor2var`，立即报错（第 473-478 行）。这是「发射器必须为输出张量建立变量映射」的硬约束，保证下游指令能拿到这个输出。

#### 4.3.4 代码实践（源码阅读型）

1. **目标**：手动推演一条指令的「派单」过程，理解 `issubclass` 与「最特化优先」。
2. **步骤**：
   - 假设当前 target 是 `nvgpu_sm90a`，要发射一条 `AddInst`（它是 `ElementwiseBinaryBaseInst` 的子类）。
   - 在 `REGISTRY` 里找到键 `ElementwiseBinaryBaseInst`（因 `issubclass(AddInst, ElementwiseBinaryBaseInst)` 为真），它的桶是 `{gpgpu_any: ElementwiseBinaryInstEmitter}`（见 [elementwise.py:35-36](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/elementwise.py#L35-L36)）。
   - 走 `match_target(nvgpu_sm90a, [gpgpu_any])`：`nvgpu_sm90a.supports(gpgpu_any)` 为真，唯一候选即当选。
3. **观察现象**：桶里只有一个 target，所以「最特化优先」未触发分歧；但流程完整。
4. **进阶**：再假设要发射 `DotInst`，它在 [cuda/mma_dot.py:27](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/cuda/mma_dot.py#L27) 注册了 `target=nvgpu_sm70`，而 `SimtDotInst` 在 [cuda/simt_dot.py:24](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/cuda/simt_dot.py#L24) 注册了 `target=gpgpu_any`。请思考：`DotInst` 与 `SimtDotInst` 是不同指令类，它们各自走自己的桶——这体现 Tilus 用「不同指令类」而非「同指令多 target」来区分 MMA 路径与 SIMT 路径。
5. **预期结果**：能口头复述「先 issubclass 定桶、再 supports+max 定 target」两步，并说出 `break` 取首个命中桶这一细节。

#### 4.3.5 小练习与答案

**练习 1**：当前 target 是 `nvgpu_sm80`，某指令的桶里有 `{gpgpu_any: E1, nvgpu_sm90a: E2}`。会选哪个发射器？为什么？

> **答案**：选 `E1`（`gpgpu_any`）。因为 `nvgpu_sm80.supports(nvgpu_sm90a)` 为**假**（sm80 跑不了 sm90a 专属指令），`nvgpu_sm90a` 被过滤掉；只剩 `gpgpu_any`，`match_target` 返回它。这体现了「supports 先过滤、再取最特化」。

**练习 2**：为什么 `check_emitter_existence` 要在编译早期跑一遍，而不是等 `visit_Instruction` 自然抛错？

> **答案**：为了让「缺发射器」的错误信息**集中且友好**——一次性列出所有缺发射器的指令及其已注册 target（提示你可能是 target 选错），而不是在发射到第 N 条指令时才抛一句 `Can not resolve the emitter for ...`，让用户难以判断是哪类指令、为何缺失。

---

### 4.4 实例精读：CastInst 发射器如何按 dtype 选择 PTX 指令

#### 4.4.1 概念说明

前面三节是「机制」，本节用一条真实指令 `CastInst` 把机制串起来，并直接服务于本讲的实践任务。`CastInst` 表示「把一个寄存器张量从 `src_dtype` 转成 `dst_dtype`」，比如把 `int8` 权重转成 `float16` 参与 MMA。

cast 的难点在于：**最朴素的逐元素转换太慢**。一个 `int8 → float16`，朴素做法是逐元素读一个 int8、写成 float16。但 NVIDIA GPU 有 PTX/硬件级的位操作指令（`prmt` 字节重排、`lop3` 三输入逻辑运算、`__hsub2/__hmul2/__hfma2` 半精度向量运算），可以**一次处理 4 个甚至 8 个元素**。于是 Tilus 的策略是：

- 维护一张「特化表」`specialized_cast: Dict[(src_dtype, dst_dtype), 实现函数]`。
- `emit` 时查表：命中就用特化的向量化实现；没命中就回退到逐元素的 `cast_generic`。
- 每个特化实现内部还会自检「元素数能否被向量化粒度整除」（如 `size % 4`），不满足就退回 `cast_generic`。

这种「按 dtype 分派 + 自动回退」是发射器里非常典型的优化模式。

#### 4.4.2 核心流程

```text
emit(inst):
  src, dst = inst.inputs[0], inst.register_output
  在 tensor2var 里登记 dst 的新变量
  查 (src.dtype, dst.dtype):
      ├── 命中 specialized_cast → 用 PTX 向量化实现（prmt/lop3/f16x2 …）
      │       └── 若 size 不满足对齐要求 → 内部再退回 cast_generic
      └── 未命中 → cast_generic（逐元素隐式 cast）
```

#### 4.4.3 源码精读

基类 `CastInstBaseEmitter.emit`——查表分派：

[python/tilus/backends/emitters/cast.py:73-94](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/cast.py#L73-L94) 说明：读 `src`/`dst`，为 `dst` 声明新变量并登记 `tensor2var`（这一步满足 `visit_Instruction` 的硬校验）；取 `(src_dtype, dst_dtype)`，若在 `specialized_cast` 表里则用特化实现 `impl`，否则用 `cast_generic`。

通用回退 `cast_generic`——逐元素隐式 cast：

[python/tilus/backends/emitters/cast.py:96-98](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/cast.py#L96-L98) 说明：一个简单 `for_range`，把 `src[i]` 写入 `dst[i]`，依赖 Hidet/C 层的隐式类型转换。慢但万能。

NVIDIA 特化表——填入各种 `(低精度, float16/bfloat16)` 组合：

[python/tilus/backends/emitters/cast.py:101-121](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/cast.py#L101-L121) 说明：`NvgpuCastInstEmitter.__init__` 往 `specialized_cast` 里塞了一堆条目，把 `(uint8, float16)`→`cast_u8_to_f16`、`(int8, float16)`→`cast_i8_to_f16`、`(int4b, float16)`→`cast_i4_to_f16`、`(float8_e4m3, float16)`→`cast_f8e4m3_to_f16` 等一一绑定。这张表就是「按 dtype 选 PTX 指令」的决策核心。

一个典型特化——`cast_u8_to_f16`（uint8 → float16，一次 4 个）：

[python/tilus/backends/emitters/cast.py:123-145](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/cast.py#L123-L145) 说明：先把 `src/dst` 重解释成 `uint32` 视图（4 个 uint8 拼 1 个 uint32、2 个 float16 拼 1 个 uint32）；循环每轮用 `prmt`（字节重排，把 uint8 摆到 float16 的尾数位并加上偏置 `1024`）生成两对 float16，再用 `sub_f16x2`（半精度向量减法，一次处理 2 个 float16）减掉偏置。`prmt` 与 `sub_f16x2` 就是这里选用的「PTX 特殊指令」。注意开头 `if self.size % 4 != 0: self.cast_generic(...); return`——元素数不齐 4 就退回通用路径。

带符号变体——`cast_i8_to_f16`（int8 → float16）：

[python/tilus/backends/emitters/cast.py:147-172](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/cast.py#L147-L172) 说明：int8 范围是 `-128~127`，而前述 `prmt` 技巧只对 `0~255` 的无符号值成立，所以先用 `^ 0x80808080`（异或）把 int8 偏移成 uint8（加 128），再复用无符号套路，最后把偏置从 `1024` 调成 `1024+128=1152`（`0x6480`）。这里 `lop3`-式思路与 `prmt`/`sub_f16x2` 配合，体现「用位运算把范围对齐后再套用同一套向量化模板」。

对比：AMD 发射器没有任何特化：

[python/tilus/backends/emitters/cast.py:499-503](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/cast.py#L499-L503) 说明：`AmdgpuCastInstEmitter.__init__` 里 `self.specialized_cast.update({})`——空表，于是所有 cast 都走 `cast_generic`。这正是 4.2 「同指令多 target」的价值：同一 `CastInst`，NVIDIA 走高度优化路径、AMD 走通用路径，互不干扰。

作为对照，再看一个最朴素的发射器——`ElementwiseBinaryInstEmitter`：

[python/tilus/backends/emitters/elementwise.py:35-53](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/elementwise.py#L35-L53) 说明：它不挑 dtype、不查表，直接 `for_range` 遍历输出的每个 local 元素，借助 `layout.get_global/get_local`（布局系统，U4）把「输出局部索引」翻译成「输入局部索引」（含广播），读两个输入、调 `inst.f_compute(lhs, rhs)` 写输出。它展示了发射器「另一极」的形态：当指令本身足够通用时，发射器可以极其简短，复杂度被布局系统与 `f_compute` 吸收。

#### 4.4.4 代码实践（结合本讲任务）

> 这是本讲的主实践，对应任务：「在 emitters/ 中找出 CastInst 对应发射器，说明它如何根据 dtype 选择 PTX 特殊 cast 指令。」

1. **目标**：亲眼看到「同一 `CastInst`，因 target 与 dtype 不同而走不同代码路径」，并能指认选中的 PTX 指令。
2. **操作步骤（源码阅读为主）**：
   - 打开 [python/tilus/backends/emitters/cast.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/cast.py)，定位 `CastInstBaseEmitter.emit`（73-94 行）与 `NvgpuCastInstEmitter.__init__` 的特化表（101-121 行）。
   - 选定一个组合，例如 `(int8, float16)`，跟踪它绑定到 `cast_i8_to_f16`（147-172 行）。
   - 列出该函数用到的 PTX/硬件内建：`prmt`（来自 `tilus.hidet.ir.primitives.cuda.prmt`）、`sub_f16x2`（来自 `...cuda.half`）、异或 `^ 0x80808080`。在 [cast.py:39-41](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/cast.py#L39-L41) 的 import 处可确认这些内建的来源。
   - 再挑一个未被特化的组合，例如 `(float32, float16)`：它不在特化表里，故走 `cast_generic`（96-98 行），逐元素隐式转换，没有任何 PTX 特殊指令。
3. **运行型验证（可选，需 NVIDIA GPU）**：
   - 写一个最小 cast 内核：用 `register_tensor` 建一个 int8 输入与 float16 输出，用 `self.cast(...)` 连接（参考 [examples/matmul](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/matmul/) 里 `cast` 的用法）。
   - 设 `tilus.option.cache_dir("cast-cache")`，编译后在 `cast-cache/programs/*/source.cu` 里搜索 `prmt`、`sub` 指令，验证它们确实出现在 `(int8→float16)` 的生成代码中。
   - 把输入 dtype 换成 `float32` 重新编译，对比 `source.cu` 里不再出现 `prmt`，而是普通的逐元素转换。
4. **需要观察的现象**：特化组合生成向量化、含 `prmt`/`sub.f16x2` 的 CUDA；非特化组合生成朴素循环。
5. **预期结果**：能填出下表（示例）：

   | `(src, dst)` | 命中特化？ | 选用的关键 PTX 指令 | 对齐要求 |
   | --- | --- | --- | --- |
   | `(int8, float16)` | 是 → `cast_i8_to_f16` | `prmt` + `sub.f16x2`（先 `^0x80808080`） | `size % 4 == 0` |
   | `(uint4b, float16)` | 是 → `cast_u4_to_f16` | `lop3` + `prmt` + `fma.f16x2` | `size % 8 == 0` |
   | `(float8_e4m3, float16)` | 是 → `cast_f8e4m3_to_f16` | `prmt` + `lop3` + `mul.f16x2` | `size % 4 == 0` |
   | `(float32, float16)` | 否 → `cast_generic` | 无（逐元素隐式 cast） | 无 |

6. 若无 GPU 可运行，标注「待本地验证」并以源码阅读结论为准。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `cast_i8_to_f16` 要先做 `^ 0x80808080`，而 `cast_u8_to_f16` 不用？

> **答案**：`prmt` 那套「把字节摆进 float16 尾数并加偏置」的技巧，对每个字节假设其值为 `0~255`（无符号）。uint8 天然在此范围；int8 范围是 `-128~127`，直接套用会把负数解释成大正数。`^ 0x80808080` 等价于「每个字节加 128」，把 int8 平移到 `0~255`，于是能复用同一套向量化模板；代价是最终偏置要从 `1024` 改成 `1152`（多减 128）。

**练习 2**：若某个 `(src, dst)` 组合在特化表里，但运行时 `size % 4 != 0`，会发生什么？

> **答案**：该特化函数（如 `cast_u8_to_f16`）开头会判断 `if self.size % 4 != 0: self.cast_generic(src, dst); return`，主动**退回逐元素通用路径**。即「特化只在数据量满足对齐时启用」，保证正确性优先于性能。

**练习 3**：`ElementwiseBinaryInstEmitter` 为什么不需要像 cast 那样维护一张 dtype 分派表？

> **答案**：元素wise 二元运算（加、乘……）的「计算」对 dtype 不敏感——都只是「读两个操作数、调 `f_compute`、写回」，dtype 差异由具体的 `f_compute` 与隐式类型转换吸收；而 cast 的难点在「位级重排」，不同位宽必须用不同的位操作序列，所以才需要按 dtype 分派不同的向量化算法。

## 5. 综合实践

把本讲三块内容（基类能力、注册机制、target 匹配）串起来，完成一个「派单追踪」小任务：

1. **任务**：为一条 hypothetical 的新指令设计它的发射器，并预测 Tilus 会如何派单。假设你要新增一条 `MyInst`（继承自 `ElementwiseUnaryBaseInst`），实现一个发射器 `MyEmitter`。
2. **步骤**：
   - **第一步（注册）**：仿照 [elementwise.py:22-23](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/elementwise.py#L22-L23)，写 `@register_emitter(MyInst)`（target 缺省为 `gpgpu_any`），或在 `class MyInst(ElementwiseUnaryBaseInst)` 的情况下，预测它会不会**即使不注册**也能命中已有的 `ElementwiseUnaryInstEmitter`。
   - **第二步（派单预测）**：在纸上推演 `resolve_inst_emitter(MyInst)`——遍历 `REGISTRY`，`issubclass(MyInst, ElementwiseUnaryBaseInst)` 是否为真？若是，则首个命中的桶是 `ElementwiseUnaryBaseInst`（前提是它先被 import），`MyEmitter` 反而**不会被用到**（因为更具体的 `MyInst` 桶若后注册，`break` 取的是先注册的基类桶）。
   - **第三步（验证）**：写一小段 Python，构造一个含 `MyInst` 的 `Function`，调 `FunctionCodegen(...).resolve_inst_emitter(MyInst)`，打印返回的发射器类名，验证你的预测。
3. **观察与结论**：你会直观体会到两个设计后果——(a)「按基类注册 + issubclass」让新子类零成本复用发射器；(b) 但若既注册基类又注册子类，**import 顺序（即 `REGISTRY` 的插入顺序）决定谁胜出**，因此新增更特化的发射器时要注意注册粒度，避免被基类桶「抢先」。
4. **进阶（可选）**：让 `MyEmitter.emit` 里调一次 `self.get_or_allocate_var(inst.register_output)` 与 `self.sync()`，分别在生成的 `source.cu` 里找到对应的变量声明与同步语句，验证「基类通用能力」确实落到了最终 CUDA 里。

> 说明：本实践以源码阅读 + 少量 Python 推演为主；若需端到端编译验证，参考 u8-l5「自定义 Pass 与新增指令」里用 `InstantiatedScript._jit_instance_for` 做端到端测试的范式。

## 6. 本讲小结

- **`BaseInstEmitter` 是发射器基类**：继承 hidet 的 `StmtBuilder`，提供 `get_or_allocate_var`（张量↔变量惰性映射）、`sync`（委托同步上下文）、线程组查询、`single_thread` 等通用能力；具体发射器只需实现 `emit(inst)`。
- **`REGISTRY` 是「指令 → {target → 发射器}」的两层全局表**，由 `@register_emitter(inst_cls, target=...)` 在 import 期填充；缺省 target 为 `gpgpu_any`；同 `(指令, target)` 重复登记会在 import 期抛 `ValueError`。
- **注册的三种粒度**：同指令多 target（如 `CastInst` 分 NVIDIA/AMD）、多指令共享一个发射器（装饰器叠放）、按基类注册覆盖所有子类（如 `ElementwiseUnaryBaseInst`，靠 `issubclass` 命中）。
- **派单分两步**：`resolve_inst_emitter` 用 `issubclass` 定桶（取首个命中、`break`），`match_target` 用 `Target.supports` 过滤后取算力最高者；`check_emitter_existence` 在编译早期汇总「缺发射器」错误。
- **`visit_Instruction` 的硬约束**：发射器必须为 `inst.output` 在 `tensor2var` 里建映射，否则报错；这保证了下游指令能拿到输出。
- **`CastInst` 是机制的最佳样本**：它用一张 `(src_dtype, dst_dtype)` 特化表在「PTX 向量化实现（prmt/lop3/f16x2）」与「逐元素通用 cast」间分派，且每个特化内部按对齐要求自动回退；同指令在 NVIDIA/AMD 上挂不同发射器，体现了 target 分发的价值。

## 7. 下一步学习建议

- **下一讲 u6-l3（EmitContexts：内存分配与同步状态）**：本讲多次提到 `self.contexts.sync_ctx`、`self.contexts.barrier_alloc_ctx`，下一讲会完整展开 `EmitContexts` 这九个上下文（共享/全局内存分配、同步、mbarrier、leader_lane、const_reg、invariant、global_view、tcgen05），解释发射器背后的「状态机」。
- **u6-l4（通用发射器：elementwise/reduce/ldst/shared_ldst）**：本讲只看了最朴素的 elementwise 与 cast，u6-l4 会精读 reduce 的跨线程规约、ldst 的全局访存、shared_ldst 与 `ldmatrix` 的关系，看到发射器如何把布局翻译成每线程的标量地址。
- **回到 U4 布局系统**：若你对 `ElementwiseBinaryInstEmitter` 里的 `layout.get_global/get_local` 感到陌生，建议回看 u4-l2/u4-l4，理解发射器是如何依赖 RegisterLayout 的 spatial/local 映射来「逐线程展开」的。
- **延伸阅读源码**：浏览 [python/tilus/backends/emitters/cuda/](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/cuda/) 下的 wgmma/tcgen05/mma_dot 等发射器，观察它们如何用 `assert_is_warp_aligned` 等断言保护硬件指令的前提条件，这是 U7（架构实践）的代码基础。
