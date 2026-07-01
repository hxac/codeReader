# Module 与 RawModule 生命周期

## 1. 本讲目标

在前面的讲义里，你已经学会用 `Module(new MyMod)` 定义一个硬件模块，并用 `ChiselStage.emitSystemVerilog` 生成 Verilog。但 `Module(...)` 这一层包裹到底替你做了什么？模块的构造体（`IO`、`:=`、`when`、`Reg`）写下的代码，是在哪一刻、被谁「收口」成内部 IR 的？为什么有的模块自带 `clock`/`reset`，有的却没有？

本讲把 `Module(new MyMod)` 这一行打开，逐层讲解 Chisel 模块的 **elaboration（细化）生命周期**。学完后你应当能够：

- 说清 `Module(...)` 从「按下构造按钮」到「产出一个 FIRRTL 组件」中间经历的 `evaluate → generateComponent → initializeInParent` 三大步骤。
- 理解 `Module`（object 工厂）、`BaseModule`（抽象基类）、`RawModule`（无隐式时钟的基类）、`Module`（class，带隐式 clock/reset）这四者的职责分工与继承关系。
- 解释构造期「握手」标志 `readyForModuleConstr` 如何保证 `Module(...)` 里恰好构造一个模块。
- 动手写一个 `RawModule` 子类，并与 `Module` 子类对比生成的 Verilog 差异。

## 2. 前置知识

在进入源码前，先建立三个直觉。这些直觉在 u1-l4、u1-l5、u2-l3 已有铺垫，这里再强调一次。

**直觉一：模块构造体「只登记，不施工」。**
你在 `class MyMod extends Module` 的花括号里写的 `val io = IO(...)`、`io.out := io.in + 1.U`、`when(...)` 等代码，本质是普通 Scala 语句。它们执行时，Chisel 只是把「这里要一个端口」「这里要一条连线」「这里要一个条件分支」**登记**到 Builder 维护的命令队列里，并不会立刻产生任何硬件或文件。真正的「施工」发生在模块构造体跑完之后。

**直觉二：`Module(...)` 是一个工厂包装，不是构造器。**
`new MyMod` 才是真正调用模块构造器（constructor）的 Scala 表达式；外层的 `Module(...)` 是 Chisel 提供的包装函数，负责在调用构造器**之前**设置全局状态、在**之后**收口并登记实例。这种「包装」是 EDSL（嵌入式 DSL）常见的模式——用一个普通函数来控制副作用的边界。

**直觉三：elaboration 是一次「自顶向下的 Scala 求值」。**
生成 Verilog 时，Chisel 会从顶层模块开始，执行它的构造体；构造体里遇到 `Module(new Child)`，就**递归**地把子模块也走一遍同样的流程。整个过程就是一段 Scala 程序的运行，运行结束后，所有模块都被收拢成一棵 IR 树（`Circuit ⊃ Component`），再交给后续阶段（Convert → CIRCT）发射。

理解了这三点，下面的源码就是在回答一个具体问题：**这个「登记 → 收口 → 登记」的流程，在代码里是怎么编排的？**

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [core/src/main/scala-2/chisel3/ModuleIntf.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala-2/chisel3/ModuleIntf.scala) | `Module.apply` 的宏入口，展成 `do_apply`，注入源信息。Scala 2 版本。 |
| [core/src/main/scala/chisel3/Module.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala) | 本讲主战场：`object Module`（工厂 + `evaluate`）、`abstract class Module`（带 clock/reset）、`BaseModule`（抽象基类）、`ImplicitClock`/`ImplicitReset` 全在这里。 |
| [core/src/main/scala/chisel3/RawModule.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/RawModule.scala) | `abstract class RawModule`：无隐式 clock/reset 的模块基类，提供 `generateComponent` 的具体实现。 |
| [core/src/main/scala/chisel3/internal/Builder.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala) | elaboration 期的全局状态机，持有 `readyForModuleConstr`、`currentModule`、`components` 等。 |
| [core/src/main/scala/chisel3/internal/firrtl/IR.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala) | 内部 FIRRTL IR 节点定义，含承载命令的 `Block` 类与 `DefModule` 等。 |

继承关系一览（本讲要建立的类型层级）：

```
HasId
  └─ BaseModule (abstract, chisel3.experimental)        // 所有模块的抽象基类
       └─ RawModule (abstract)                          // 无隐式 clock/reset，可多次 IO()
            └─ Module (abstract)  with ImplicitClock with ImplicitReset
                                                        // 自动生成 clock/reset 端口
```

> 提示：`BaseModule` 定义在 `chisel3.experimental` 包里，但与 `Module.scala` 同文件；`RawModule` 单独成文件。本讲引用 `BaseModule` 时都指向 [Module.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala) 中的定义。

---

## 4. 核心概念与源码讲解

### 4.1 生命周期全景：Module(new MyMod) 的三大步

#### 4.1.1 概念说明

把 `Module(new MyMod)` 想象成「在工厂里定制一台机器」。这台机器的生产分三个阶段：

1. **定制（evaluate）**：把图纸（`new MyMod` 的构造体）跑一遍，让机器「长出来」。期间所有零件（端口、寄存器、连线）都被登记到这台机器名下。
2. **收口（generateComponent）**：图纸跑完后，把登记的零件命名、归类，封箱成一个不可再改的 IR 组件（`DefModule`），并禁止再往里加东西。
3. **上挂（initializeInParent）**：回到父模块的作用域，把这台机器作为一个**实例**（instance）登记到父模块的命令流里，并接好它的隐式 `clock`/`reset`（如果是 `Module` 的话）。

这三步对应源码里的三个方法名：`evaluate`、`generateComponent`、`initializeInParent`。本讲后续四个小节就是在逐个拆这三步。

#### 4.1.2 核心流程

下面用伪代码画出一次 `Module(new MyMod)` 的完整时序（假设它被某个父模块的构造体调用）：

```text
Module(new MyMod)                      # 用户代码
  └─ (宏) do_apply(new MyMod)          # 注入调用处 SourceInfo
       └─ _applyImpl(new MyMod):
            ① evaluate(new MyMod):      # ── 阶段1：定制 ──
               readyForModuleConstr = true   # 举起「允许构造」旗
               运行 new MyMod 的构造器:
                   BaseModule 构造器:
                     校验 readyForModuleConstr==true   # 握手
                     readyForModuleConstr = false      # 放下旗
                     Builder.currentModule = Some(this)
                     Builder.pushBlock(_body)          # 后续命令进这个块
                   MyMod 构造体执行:
                     IO(...), :=, when, Reg ...        # 全部登记为 Command
               校验 readyForModuleConstr==false        # 确认确实构造了一个
               ② module.generateComponent()           # ── 阶段2：收口 ──
                   命名 _ids、关闭 _body、构造 DefModule
                   Builder.components += component
               module.moduleBuilt()                   # 触发 afterModuleBuilt 钩子
            ③ (回到父作用域)
               pushCommand(DefInstance(...))          # ── 阶段3：上挂 ──
               module.initializeInParent()            #   接 clock/reset 等
            return module
```

注意三个反直觉点：

- **「旗子」`readyForModuleConstr` 是个握手信号**：`evaluate` 先把它举起（`true`），`BaseModule` 构造器必须看到它举起才肯构造，构造后立刻放下（`false`）。这样既防止「不包 `Module(...)` 直接 `new`」，也防止「一个 `Module(...)` 里 `new` 了两个模块」。
- **命令不是直接进模块，而是进 `Block`**：`Builder.pushBlock(_body)` 之后，`addCommand` 会把命令塞进当前 `Block`。一个模块的命令集合由 `Block` 持有。
- **`generateComponent` 在 `evaluate` 内部就被调用了**，不是延迟到后续 Phase。收口发生在「模块构造体刚跑完」的那一刻。

#### 4.1.3 源码精读

整个 `Module(new MyMod)` 的编排入口是 `object Module` 的 `_applyImpl`。先看它如何把三步串起来（行号对应 [Module.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala)）：

[core/src/main/scala/chisel3/Module.scala:36-62](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L36-L62) —— `Module._applyImpl`：先 `evaluate` 拿到模块实例，再在父作用域里 `pushCommand(DefInstance(...))` 登记实例，最后 `initializeInParent` 接线。注意 `DefClass`/`DefObject` 分支是给「对象模型」（properties，见 u8-4）用的，普通 RTL 模块走的是 `DefInstance` 分支。

```scala
private[chisel3] def _applyImpl[T <: BaseModule](bc: => T)(implicit sourceInfo: SourceInfo): T = {
  val module: T = evaluate[T](bc)            // ① 定制 + 收口（generateComponent 在内部）
  if (Builder.currentModule.isDefined && module._component.isDefined) {
    ...
    pushCommand(DefInstance(sourceInfo, module, component.ports))  // ③ 上挂：登记实例
    module.initializeInParent()              // ③ 上挂：接线（clock/reset 等）
  }
  module
}
```

`evaluate` 是阶段 1 的核心，下一节详读。这里先确认 `_applyImpl` 的边界条件：只有当**外层已有 currentModule**（即这次调用发生在某个父模块构造体内）且本模块确实产出了 `_component` 时，才登记实例。这解释了为什么「顶层模块」不会产生 `DefInstance`——它没有父模块。

而用户写的 `Module(...)`，其实是一个宏。在 Scala 2 下，`Module.apply` 被宏 `InstTransform` 改写成带调用处源信息的 `do_apply`：

[core/src/main/scala-2/chisel3/ModuleIntf.scala:19-22](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala-2/chisel3/ModuleIntf.scala#L19-L22) —— `apply` 是宏入口，`do_apply` 是宏展开后真正调用的方法，它把编译期捕获的 `SourceInfo`（文件名 + 行号）作为隐式参数传下去，最终委派给 `_applyImpl`。这套「宏注入源信息」的机制在 u7-l2 会详讲，这里只需知道：`Module(new MyMod)` 等价于 `do_apply(new MyMod)(调用处的 SourceInfo)`。

#### 4.1.4 代码实践

**实践目标**：把上面的时序图与源码对上号，确认三步方法的调用关系。

**操作步骤**：

1. 打开 [Module.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala)。
2. 定位 `object Module`（约第 34 行）→ `_applyImpl`（约第 36 行）→ `evaluate`（约第 65 行）。
3. 在 `evaluate` 内部找到对 `module.generateComponent()` 的调用（约第 92 行），以及末尾对 `module.moduleBuilt()` 的调用（约第 106 行）。
4. 在 `_applyImpl` 内部找到 `pushCommand(DefInstance(...))` 与 `module.initializeInParent()`（约第 56-58 行）。

**需要观察的现象**：`generateComponent` 出现在 `evaluate` 内部，而 `DefInstance` 与 `initializeInParent` 出现在 `evaluate` **返回之后**的 `_applyImpl` 里——印证了「收口在定制阶段内完成，上挂在定制阶段之后」。

**预期结果**：你能用一句话描述「`Module(new MyMod)` = `evaluate`（含 `generateComponent`）+ 父作用域登记 `DefInstance` + `initializeInParent`」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_applyImpl` 里登记 `DefInstance` 的 `if` 条件要同时检查 `Builder.currentModule.isDefined` 和 `module._component.isDefined`？

> **答案**：`currentModule.isDefined` 表示当前正处在某个父模块的构造体内（即这次实例化不是顶层模块）；`_component.isDefined` 表示本模块确实产出了一个 IR 组件（极少数「伪模块」`PseudoModule` 不产出）。两个条件都满足，才需要、也才能够向父模块的命令流里登记一个实例。

**练习 2**：如果用户写 `val m = new MyMod`（忘了包 `Module(...)`），会在哪一行报错？

> **答案**：会在 `BaseModule` 构造器里报错，提示 "attempted to instantiate a Module without wrapping it in Module()"。具体位置见 4.3 节的握手校验（[Module.scala:486-492](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L486-L492)）。

---

### 4.2 Module 工厂方法与 evaluate：构造期的编排

#### 4.2.1 概念说明

`object Module` 是模块实例化的「工厂单例」。它对外暴露 `apply`（宏），对内用 `evaluate` 完成最难的工作：**在一个受控的、可回滚的全局状态窗口里，运行模块构造器**。

「可回滚的全局状态窗口」是关键词。Chisel 的 Builder 持有一堆可变全局状态（当前模块、命令块栈、命名前缀栈等）。`evaluate` 必须保证：进入子模块构造前**保存**这些状态，构造中让子模块「霸占」它们，构造结束后**恢复**到父模块的上下文——否则子模块的命令会污染父模块。这个保存/恢复是用 `Builder.State.guard` 实现的。

#### 4.2.2 核心流程

`evaluate` 的执行步骤：

```text
evaluate(bc):
  1. 校验并设置 readyForModuleConstr = true     # 举起握手旗
  2. elaborationTrace.pushModule()               # 记录调用栈（用于报错）
  3. 保存父模块的命名前缀栈 savedPrefixStack
  4. Builder.State.guard(defaultState) {         # 进入受控状态窗口
       val module = bc                           # ★ 运行模块构造器（副作用全在此）
       校验 whenDepth == 0                       # when/elsewhen 必须配对闭合
       校验 readyForModuleConstr == false        # 确认构造器确实执行了
       componentOpt = module.generateComponent() # ★ 收口
       Builder.components += component           # 把组件登记到全局
       恢复命名前缀栈
     }
  5. elaborationTrace.popModule(name)
  6. module.moduleBuilt()                        # 触发 afterModuleBuilt 钩子
```

#### 4.2.3 源码精读

[core/src/main/scala/chisel3/Module.scala:65-108](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L65-L108) —— `Module.evaluate` 的完整实现。下面摘关键部分：

```scala
private[chisel3] def evaluate[T <: BaseModule](bc: => T)(implicit sourceInfo: SourceInfo): T = {
  if (Builder.readyForModuleConstr) {
    throwException("Error: Called Module() twice without instantiating a Module." + ...)  // 防「双重 Module()」
  }
  Builder.readyForModuleConstr = true          // ① 举起旗
  Builder.elaborationTrace.pushModule()        // ② 压栈
  val savedPrefixStack = Builder.getModulePrefixStack

  val module = Builder.State.guard(Builder.State.default) {   // ④ 受控窗口
    val module: T = bc                          // ★ 运行构造器
    if (Builder.whenDepth != 0) throwException(...)           // 校验 when 闭合
    if (Builder.readyForModuleConstr) throwException(         // 校验确实构造了
      "Error: attempted to instantiate a Module, but nothing happened. ...")

    val componentOpt = module.generateComponent()             // ★ 收口（见 4.4）
    for (component <- componentOpt) {
      Builder.components += component
    }
    ...
    module
  }

  Builder.elaborationTrace.popModule(module.desiredName)
  module.moduleBuilt()                          // ⑥ 触发钩子
  module
}
```

几个要点：

- **`bc: => T` 是按名调用参数**（call-by-name）。`bc`（即 `new MyMod`）不会在传入时立刻求值，而是延迟到 `val module: T = bc` 这一行才真正执行模块构造器。这让 `evaluate` 能在执行构造器**前后**插入准备与收尾工作。
- **两个 `readyForModuleConstr` 校验**分别防御两类错误：进入时若已是 `true`，说明上一个 `Module(...)` 还没收口（双重嵌套）；退出时若仍是 `true`，说明构造体里根本没构造模块（比如把一个已有实例再包一层）。这两条错误信息在实战中很常见，值得记住。
- **`Builder.State.guard`** 是状态隔离的边界：窗口内对全局状态的修改，在异常时会回滚，正常时按规则合并。这保证了子模块的副作用不会泄漏到父模块。

`object Module` 还提供了若干便捷访问器，让模块**内部**的代码能拿到当前隐式信号：

[core/src/main/scala/chisel3/Module.scala:111-120](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L111-L120) —— `Module.clock` / `Module.reset` 等返回 Builder 里记录的当前隐式 clock/reset。当你在模块里写 `Reg(UInt(8.W))` 而不指定时钟时，寄存器就是从这里拿到隐式 `clock` 的。

#### 4.2.4 代码实践

**实践目标**：亲手触发 `evaluate` 的两条错误校验，理解握手旗的防御意图。

**操作步骤**：

1. 准备一个最小模块（用 `ChiselStage.emitSystemVerilog` 触发 elaboration，参见 u1-l4）：

   ```scala
   // 示例代码（非项目原有）
   import chisel3._

   class Child extends Module {
     val io = IO(new Bundle { val out = Output(UInt(8.W)) })
     io.out := 1.U
   }

   class Bad1 extends Module {
     val io = IO(new Bundle { val out = Output(UInt(8.W)) })
     val c = Module(new Child)       // 正常
     val d = Module(new Child)       // 也正常——这是两次独立的 Module() 调用
     io.out := c.io.out + d.io.out
   }
   ```

2. 现在故意制造「双重 Module()」错误：在 `Child` 的构造体里再写一个未闭合的 `Module(...)`（例如把 `Module(new Child)` 写成嵌套且不接住），或更简单地——直接在模块外层连续调用两次 `Module(...)` 但中间不结束构造。**待本地验证**：具体触发哪条信息取决于写法，但报错关键字是 `"Called Module() twice without instantiating a Module"`。

3. 在源码里把这条字符串在 [Module.scala:67-70](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L67-L70) 找到，对照理解。

**需要观察的现象**：Chisel 抛出的异常带有调用处的 `SourceInfo`（文件名 + 行号），这正是 `do_apply` 宏注入的源信息。

**预期结果**：能复现并定位握手校验报错；若环境无法运行，明确标注「待本地验证」并写出预期报错文本。

#### 4.2.5 小练习与答案

**练习 1**：`bc: => T` 为什么必须用按名调用（`=>`），而不是普通的 `bc: T`？

> **答案**：若用按值调用，`new MyMod` 会在**传入 `evaluate` 之前**就执行，那时 `readyForModuleConstr` 还没被举起、`State.guard` 窗口还没打开，模块构造器里的 `IO`、`Reg` 等就会在错误的全局状态下运行。按名调用把构造器的执行时机推迟到 `evaluate` 设置好一切之后。

**练习 2**：`evaluate` 末尾的 `module.moduleBuilt()` 与 `generateComponent` 是什么关系？

> **答案**：`generateComponent` 负责把模块**收口成 IR 组件**（不可再改）；`moduleBuilt` 是收口**之后**的钩子，运行用户用 `afterModuleBuilt { ... }` 注册的延迟生成器（见 [RawModule.scala:60-63](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/RawModule.scala#L60-L63) 与 [RawModule.scala:230](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/RawModule.scala#L230)）。此时模块自身已不能再被修改，但可以基于它的 Definition 触发其它模块的生成（如单元测试）。

---

### 4.3 BaseModule：所有模块的抽象基类与构造期握手

#### 4.3.1 概念说明

`BaseModule` 是 Chisel 里**一切模块**（`RawModule`、`Module`、`BlackBox`、`Definition`/`Instance` 里的克隆模块等）的共同抽象基类。它定义了一个模块在 elaboration 期需要持有的全部可变状态：唯一标识、父模块引用、端口表、命令块、命名空间、是否已关闭等。

最关键的是 `BaseModule` 的**构造器主体**——它在 `new MyMod` 执行时立即运行，负责与 Builder 完成「握手」：把 `Builder.currentModule` 指向自己，并把自己的命令块压入 Builder 的块栈。这就是为什么你在模块里写 `io.in := x` 时，Builder 知道这条命令属于「当前模块」。

#### 4.3.2 核心流程

`BaseModule` 构造器（每个子类构造时都会经过）做的事：

```text
new BaseModule 子类():
  _parent.foreach(_.addId(this))              # 把自己登记到父模块的 _ids 里
  this match:
    case PseudoModule => ()                    # 伪模块跳过握手
    case other =>
      若 !readyForModuleConstr => 抛异常        # 握手校验：必须被 Module(...) 包裹
  若 hasDynamicContext:
    readyForModuleConstr = false               # 放下握手旗
    Builder.currentModule = Some(this)         # 当前模块指向自己
    getBody.foreach(Builder.pushBlock(_))      # 把自己的 _body 压入块栈
```

之后模块构造体里每一次 `addCommand`，都会把命令塞进**栈顶的 Block**——也就是刚压入的 `_body`。

#### 4.3.3 源码精读

[core/src/main/scala/chisel3/Module.scala:423](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L423) —— `BaseModule` 的声明。它混入了 `HasId`（有全局唯一 id，见 u4-l1）、`IsInstantiable`（可被 Definition/Instance 体系实例化，见 u8-l1）、`ReflectSelectable`。

构造器里的握手逻辑在：

[core/src/main/scala/chisel3/Module.scala:486-498](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L486-L498) —— 这是「不包 `Module(...)` 就 `new`」的报错点，以及握手旗的放下与 `currentModule`/块栈的设置。

```scala
this match {
  case _: PseudoModule =>
  case other =>
    if (!Builder.readyForModuleConstr) {
      throwException("Error: attempted to instantiate a Module without wrapping it in Module().")
    }
}
if (Builder.hasDynamicContext) {
  readyForModuleConstr = false               # 放下旗
  Builder.currentModule = Some(this)         # 当前模块 = 自己
  getBody.foreach(Builder.pushBlock(_))      # 命令块压栈
}
```

结合 4.2 的 `evaluate`，你能看到完整的握手往返：

| 时刻 | `readyForModuleConstr` | 动作 |
| --- | --- | --- |
| `evaluate` 进入 | `false` → `true` | 举起旗，允许接下来的 `new` |
| `BaseModule` 构造器 | 校验为 `true`，然后 → `false` | 确认被包裹，放下旗 |
| `evaluate` 收口前 | 校验为 `false` | 确认确实构造了一个模块 |

`BaseModule` 持有的关键可变状态（用于后续 `generateComponent`）：

- [Module.scala:503](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L503) `_closed: Boolean` —— 模块是否已收口，收口后不能再 `addCommand`/`addId`。
- [Module.scala:559](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L559) `_namespace: Namespace` —— 模块**私有**命名空间，避免本模块内信号名冲突。
- [Module.scala:563-568](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L563-L568) `_ids: ArrayBuffer[HasId]` + `addId` —— 记录本模块内创建的所有「有 id 的对象」（端口、线、寄存器、子实例……），收口时统一命名。
- [Module.scala:584](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L584) `_ports: ArrayBuffer[(Data, SourceInfo)]` —— 端口表，由 `IO(...)` 调用 `_bindIoInPlace` 填充。
- [Module.scala:467-469](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L467-L469) `_body: Block` —— 承载命令的块；`hasBody` 在 `RawModule` 里被覆盖为 `true`，故 `RawModule` 及其子类（含 `Module`）都有 body。

`generateComponent` 与 `initializeInParent` 在 `BaseModule` 里是**抽象**的：

[core/src/main/scala/chisel3/Module.scala:662-669](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L662-L669) —— `BaseModule` 只声明 `generateComponent(): Option[Component]` 与 `initializeInParent(): Unit` 为抽象方法。具体实现由 `RawModule`（下一节）给出，`Module` 再覆盖 `initializeInParent` 来接 clock/reset。

#### 4.3.4 代码实践

**实践目标**：观察「命令进哪个 Block」，理解 `pushBlock` 的作用。

**操作步骤**：

1. 打开 [IR.scala:406-437](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala#L406-L437)，阅读 `class Block`：它用 `_commandsBuilder` 收集命令，`close()` 后固化成 `_commands: Seq[Command]`。
2. 打开 [RawModule.scala:78-86](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/RawModule.scala#L78-L86)，阅读 `addCommand`：它要求模块未关闭，且 `Builder.currentBlock` 有值，然后把命令交给当前块。

   ```scala
   private[chisel3] def addCommand(c: Command): Unit = {
     require(!_closed, "Can't write to module after module close")
     require(Builder.currentBlock.isDefined, "must have block set")
     Builder.currentBlock.get.addCommand(c)
   }
   ```

3. 串联 4.3.3 的 `Builder.pushBlock(_body)`：模块构造器把 `_body` 压栈后，`currentBlock` 就是 `_body`，于是构造体里所有命令都进了这个块。

**需要观察的现象**：`addCommand` 并不直接把命令存进模块字段，而是交给「Builder 当前块」。这意味着命令的归属由 Builder 的块栈动态决定，而非静态绑定——这正是 `when` 块、`withClock` 块能临时切换命令容器的基础。

**预期结果**：你能解释「为什么 `io.out := io.in` 这条命令最终落在 `MyMod._body` 里」——因为 `BaseModule` 构造器把 `_body` 压成了栈顶。

#### 4.3.5 小练习与答案

**练习 1**：`PseudoModule` 为什么能绕过握手校验？

> **答案**：`PseudoModule` 是「伪模块」，如 `Instance`/`Definition` 体系内部的 `InstanceClone`、`ModuleClone` 等。它们不是用户用 `Module(new ...)` 构造的真模块，而是对已有模块定义的引用/克隆，创建它们时并不需要 `readyForModuleConstr` 旗子。源码在握手 `match` 里把它们单列出来跳过校验（[Module.scala:486-492](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L486-L492)）。

**练习 2**：模块收口（`_closed = true`）后，再调用 `addCommand` 会怎样？

> **答案**：`addCommand` 的第一行 `require(!_closed, "Can't write to module after module close")` 会抛 `IllegalArgumentException`。这就是「收口后不可修改」的强制保障。

---

### 4.4 generateComponent：关闭模块并产出 FIRRTL 组件

#### 4.4.1 概念说明

`generateComponent` 是阶段 2「收口」的具体实现，由 `RawModule` 提供（`BaseModule` 里是抽象的）。它的工作是：在模块构造体跑完之后，把这一路登记下来的所有 id（端口、线、寄存器、子实例等）**命名**，把命令块**关闭**，最终封装成一个内部 FIRRTL 组件 `DefModule`，存入 `_component` 字段。

一句话概括：**`generateComponent` 把「一堆待命名的 Scala 对象」固化成「一个不可改的 IR 节点」**。

#### 4.4.2 核心流程

`RawModule.generateComponent` 的步骤：

```text
generateComponent():
  1. 校验 ! _closed                             # 不能收口两次
  2. for (id <- _ids) nameId(id)                 # 第一遍：给已登记的 id 命名
  3. evaluateAtModuleBodyEnd()                   # 跑 atModuleBodyEnd 钩子（可补端口）
  4. _closed = true                              # ★ 关闭，此后不可再加命令/端口
  5. checkPorts()                                # 校验所有端口都能命名
  6. for (id <- _ids.drop(numInitialIds)) nameId(id)  # 第二遍：给钩子新增的 id 命名
  7. 由 _ports 构造 firrtlPorts: Seq[Port]
  8. _body.close()                               # 固化命令 Seq
  9. component = DefModule(this, name, _isPublic, enabledLayers, firrtlPorts, _body)
 10. _component = Some(component); return it
```

注意命名分两遍：第一遍处理构造体产生的 id；中间执行 `atModuleBodyEnd` 钩子（这些钩子可能新增端口/线）；第二遍处理钩子新增的 id。这是为了支持「在模块体末尾再补端口」的高级用法（见 [Module.scala:1089-1094](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L1089-L1094)）。

#### 4.4.3 源码精读

[core/src/main/scala/chisel3/RawModule.scala:158-196](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/RawModule.scala#L158-L196) —— `RawModule.generateComponent` 的完整实现。关键摘录：

```scala
private[chisel3] override def generateComponent(): Option[Component] = {
  require(!_closed, "Can't generate module more than once")

  val numInitialIds = _ids.size
  for (id <- _ids) { nameId(id) }                  // 第一遍命名

  evaluateAtModuleBodyEnd()                         // atModuleBodyEnd 钩子
  _closed = true                                    // ★ 关闭

  checkPorts()                                      // 端口可命名性校验
  for (id <- _ids.view.drop(numInitialIds)) { nameId(id) }  // 第二遍命名

  val firrtlPorts = getModulePortsAndLocators.map { case (port, si, assoc) =>
    Port(port, port.specifiedDirection, assoc, si)
  }
  _body.close()                                     // 固化命令

  val component =
    DefModule(this, name, _isPublic, Builder.enabledLayers.toSeq, firrtlPorts, _body)
  _component = Some(component)
  _component
}
```

`nameId` 是「给一个 id 分配 FIRRTL 名字」的派发函数，按 id 的种类（端口、寄存器、线、子模块、内存、断言、printf……）分别处理：

[core/src/main/scala/chisel3/RawModule.scala:111-156](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/RawModule.scala#L111-L156) —— `nameId`。对 `Data` 类型的 id，它根据 `topBinding`（绑定类型，见 u4-l3）决定默认名与命名方式。例如：

- `PortBinding` → 默认名 `"PORT"`，并设置端口引用 `ModuleIO(this, ...)`；
- `RegBinding` → 默认名 `"REG"`；
- `WireBinding` → 默认名 `"_WIRE"`；
- `OpBinding`（运算结果节点）→ 默认名 `"_T"`（这就是生成的 Verilog 里满眼 `_T_1`、`_T_2` 的由来）；
- 字面量（`LitBinding`）→ 不命名（走 `case _` 分支）。

最终产出的 `DefModule` 是内部 FIRRTL IR 里「模块定义」节点：

[core/src/main/scala/chisel3/RawModule.scala:190-191](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/RawModule.scala#L190-L191) —— 构造 `DefModule(this, name, _isPublic, Builder.enabledLayers.toSeq, firrtlPorts, _body)`。它把模块名、是否公开、启用的 layer、端口列表、承载命令的 `_body` 全部打包。这个 `DefModule` 随后在 `evaluate` 里被 `Builder.components += component` 收进全局组件列表（见 [Module.scala:93-95](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L93-L95)），最终汇成顶层 `Circuit`（见 u4-l2）。

> 补充：`checkPorts`（[RawModule.scala:93-102](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/RawModule.scala#L93-L102)）会报「端口无法命名」的错误——这就是为什么端口必须是模块的 `val` 字段（这样编译器插件才能给它命名，见 u7-l1），不能是局部变量。

#### 4.4.4 代码实践

**实践目标**：跟踪 `nameId` 如何决定生成 Verilog 里的信号名，理解 `_T`、`_T_1` 的来源。

**操作步骤**：

1. 写一个有运算中间节点的模块并用 `emitSystemVerilog` 生成（参见 u1-l4 的写法）：

   ```scala
   // 示例代码（非项目原有）
   import chisel3._
   class Adder extends Module {
     val io = IO(new Bundle {
       val a, b = Input(UInt(8.W))
       val s   = Output(UInt(8.W))
     })
     io.s := io.a + io.b        // 这个 + 会产生一个 OpBinding 中间节点
   }
   // (new chisel3.stage.ChiselStage).emitSystemVerilog(new Adder)
   ```

2. 在生成的 Verilog 里找到对应 `io.a + io.b` 的中间线，名字通常形如 `_T`。

3. 回到源码 [RawModule.scala:138-139](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/RawModule.scala#L138-L139)：`OpBinding(_, _) => id._forceName(default = "_T", _namespace)` —— 这就是把运算节点命名为 `_T` 的地方。

**需要观察的现象**：未显式 `suggestName` 的运算结果、连线，在 Verilog 里都叫 `_T`、`_T_1`……；而端口、用 `val io = IO(...)` 接住的信号会有可读名字。

**预期结果**：你能把 Verilog 里的 `_T` 与源码 `nameId` 的 `OpBinding` 分支一一对应。**待本地验证**：实际生成的 `_T` 数量取决于 firtool 的优化是否把它们折叠。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `nameId` 要跑两遍（构造体的 id 一遍，`atModuleBodyEnd` 钩子的 id 另一遍）？

> **答案**：因为 `atModuleBodyEnd` 钩子（在构造体之后执行）可能新增端口或线，这些新 id 也需要命名。但它们必须在 `_closed = true` 之前创建，否则会触发「模块关闭后写入」错误。所以流程是：先命名已知 id → 跑钩子（产生新 id）→ 关闭 → 再命名新 id。两遍的分割点 `numInitialIds` 记录了第一遍结束时 `_ids` 的大小（[RawModule.scala:162](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/RawModule.scala#L162) 与 [:176](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/RawModule.scala#L176)）。

**练习 2**：`generateComponent` 返回 `Option[Component]`，何时返回 `None`？

> **答案**：`RawModule`/`Module` 的实现总是返回 `Some(component)`。`Option` 是为了照顾 `BaseModule` 的其它子类——某些「伪模块」（如 `ViewParent`、`InstanceClone`）不产出真实组件，返回 `None`。`_applyImpl` 里 `module._component.isDefined` 的判断（[Module.scala:42](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L42)）就是用来跳过这些不产出组件的模块的。

---

### 4.5 Module 类与 RawModule：隐式 clock/reset 的分野

#### 4.5.1 概念说明

到目前为止讲的 `evaluate`、`BaseModule`、`generateComponent` 都属于「通用模块机制」，与 clock/reset 无关。真正区分 `Module` 和 `RawModule` 的是：

- **`RawModule`**：没有任何隐式 clock/reset。你写 `Reg(...)` 必须显式用 `withClock(...)` 指定时钟，否则报错；模块端口里也不会自动出现 `clock`/`reset`。它适合描述组合逻辑、多时钟域设计、或完全自定义端口（含多个 `IO()`）。
- **`Module`**：在 `RawModule` 基础上混入 `ImplicitClock` + `ImplicitReset`，**构造器自动**为你创建两个输入端口 `clock` 和 `reset`，并把它们设为模块内的隐式 clock/reset。于是 `Reg(...)` 无需指定时钟就会接到这个 `clock`，`RegInit(...)` 的复位也接到这个 `reset`。

两者是继承关系：`Module extends RawModule`。所以 `Module` **复用**了 `RawModule` 的全部生命周期（包括 `generateComponent`），只是多了 clock/reset 的自动装配。

#### 4.5.2 核心流程

`Module` 类构造器（在 `BaseModule` 握手之后、用户构造体之前/之中执行）做的事：

```text
class Module extends RawModule with ImplicitClock with ImplicitReset:
  （ImplicitClock / ImplicitReset 的字段初始化）
    Builder.currentClock  = Some(Delayed(implicitClock))
    Builder.currentReset  = Some(Delayed(implicitReset))

  final val clock = IO(Input(Clock())) suggestName("clock")   # 创建 clock 端口
  final val reset = IO(Input(mkReset))     suggestName("reset")   # 创建 reset 端口（类型由 mkReset 决定）

  override def implicitClock  = clock
  override def implicitReset  = reset
```

而 `initializeInParent`（阶段 3「上挂」的一部分）在 `Module` 里被覆盖，负责把这两个端口接到父模块的隐式 clock/reset：

```text
override def initializeInParent():
  super.initializeInParent()
  clock := Builder.forcedClock           # 把自己的 clock 接到父作用域的隐式 clock
  reset := Builder.forcedReset           # 把自己的 reset 接到父作用域的隐式 reset
```

#### 4.5.3 源码精读

[core/src/main/scala/chisel3/Module.scala:232-243](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L232-L243) —— `abstract class Module` 的声明与隐式 clock/reset 端口的创建。注意 `clock`、`reset` 都是 `final val`，且都是用 `IO(Input(...))` 创建的真实端口：

```scala
abstract class Module extends RawModule with ImplicitClock with ImplicitReset {
  def resetType: Module.ResetType.Type = Module.ResetType.Default

  final val clock: Clock = IO(Input(Clock()))(this._sourceInfo).suggestName("clock")
  final val reset: Reset = IO(Input(mkReset))(this._sourceInfo).suggestName("reset")

  override protected def implicitClock: Clock = clock
  override protected def implicitReset: Reset = reset
```

`reset` 的类型不是固定的，而是由 `mkReset` 根据复位策略与是否顶层决定：

[core/src/main/scala/chisel3/Module.scala:262-274](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L262-L274) —— `mkReset`。默认策略下：非顶层模块用未推断类型 `Reset()`（让后续综合决定同步还是异步），顶层模块用 `Bool()`（同步复位）。也可通过混入 `RequireAsyncReset`/`RequireSyncReset`（[RawModule.scala:236-243](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/RawModule.scala#L236-L243)）强制。

```scala
private[chisel3] def mkReset: Reset = resetType match {
  case Module.ResetType.Default => {
    val inferReset = (_parent.isDefined || Builder.inDefinition)
    if (inferReset) Reset() else Bool()        // 顶层用 Bool，非顶层用未推断 Reset
  }
  case Module.ResetType.Uninferred   => Reset()
  case Module.ResetType.Synchronous  => Bool()
  case Module.ResetType.Asynchronous => AsyncReset()
}
```

`ImplicitClock` / `ImplicitReset` 这两个 trait 负责把「用户指定的 clock/reset」登记为 Builder 的隐式值：

[core/src/main/scala/chisel3/Module.scala:307-313](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L307-L313) 与 [Module.scala:336-342](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L336-L342) —— 它们用 `Delayed(implicitClock)`/`Delayed(implicitReset)` 把抽象成员 `implicitClock`/`implicitReset` 延迟绑定到 `Builder.currentClock`/`Builder.currentReset`。`Delayed` 是必要的，因为 `implicitClock` 是 `def`，在 trait 初始化时真正的 `clock` `val` 还没赋值（Scala 初始化顺序问题，注释里有详细说明）。

最后，接线发生在覆盖后的 `initializeInParent`：

[core/src/main/scala/chisel3/Module.scala:279-285](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L279-L285) —— 把模块的 `clock`/`reset` 端口连到父作用域的隐式 clock/reset（`Builder.forcedClock`/`Builder.forcedReset`）。对比 `RawModule` 的 `initializeInParent` 是空实现（[RawModule.scala:232](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/RawModule.scala#L232)），这正是 `Module` 自动接时钟、`RawModule` 不接的根本原因。

```scala
private[chisel3] override def initializeInParent(): Unit = {
  implicit val sourceInfo = UnlocatableSourceInfo
  super.initializeInParent()
  clock := _override_clock.getOrElse(Builder.forcedClock)
  reset := _override_reset.getOrElse(Builder.forcedReset)
}
```

> 这也解释了 4.1 时序图里阶段 3「上挂」的最后一步：`_applyImpl` 调 `module.initializeInParent()` 时，对 `Module` 会额外产生两条 `clock := ...`、`reset := ...` 命令，登记到父模块的命令流里。

#### 4.5.4 代码实践（本讲综合实践）

**实践目标**：分别用 `Module` 和 `RawModule` 实现功能等价的模块，对比端口与接线差异，亲手验证本讲的全部结论。

**操作步骤**：

1. 写两个等价的「带一个寄存器、输出寄存器值」的模块：

   ```scala
   // 示例代码（非项目原有）
   import chisel3._

   // 版本 A：用 Module，自动带 clock/reset
   class RegModuleA extends Module {
     val io = IO(new Bundle { val in  = Input(Bool()); val out = Output(Bool()) })
     val r = RegNext(io.in)     // 自动接到隐式 clock，复位用隐式 reset
     io.out := r
   }

   // 版本 B：用 RawModule，手动声明 clock 并显式指定
   class RegModuleB extends RawModule {
     val clk   = IO(Input(Clock()))
     val in    = IO(Input(Bool()))
     val out   = IO(Output(Bool()))
     val r = withClock(clk)(RegNext(in))   // 必须显式指定时钟，否则报错
     out := r
   }
   ```

2. 用 `ChiselStage.emitSystemVerilog` 分别生成两个模块的 Verilog（写法见 u1-l4）。

3. **对照源码解释 `Module(new RegModuleA)` 发生了哪几步**（这是本讲实践任务的核心要求）。请按下面的提纲写一段说明，并在源码里给出每步对应的行号：
   - **定制**：`evaluate` 在 [Module.scala:65-108](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L65-L108) 举起 `readyForModuleConstr`（[:72](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L72)），运行 `new RegModuleA`。
   - **握手**：`BaseModule` 构造器在 [Module.scala:486-498](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L486-L498) 校验旗子、放下旗子、设 `currentModule`、压 `_body`。
   - **隐式端口**：`Module` 构造器在 [Module.scala:238-239](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L238-L239) 自动创建 `clock`/`reset` 端口。
   - **登记命令**：`RegNext`、`io.out := r` 经 `addCommand`（[RawModule.scala:78-82](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/RawModule.scala#L78-L82)）进入 `_body`。
   - **收口**：`generateComponent` 在 [RawModule.scala:158-196](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/RawModule.scala#L158-L196) 命名、关闭、产出 `DefModule`。
   - **上挂**：`_applyImpl` 在 [Module.scala:56-58](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L56-L58) 登记 `DefInstance` 并调用 `initializeInParent`；对 `Module`，后者在 [Module.scala:279-285](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L279-L285) 接 `clock := forcedClock`、`reset := forcedReset`。

**需要观察的现象**：

- `RegModuleA` 的 Verilog 端口表里**自动**有 `clock` 和 `reset`；`RegModuleB` 只有你手写的 `clk`、`in`、`out`，**没有** `reset`。
- `RegModuleB` 若去掉 `withClock(clk)(...)` 直接写 `RegNext(in)`，elaboration 会报「缺少隐式 clock」的错误——因为 `RawModule` 不提供隐式 clock。

**预期结果**：你能口头复述 `Module(new MyMod)` 的完整三步流程，并解释 `Module` 与 `RawModule` 在端口和接线上的差异源于 [Module.scala:238-239](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L238-L239)（自动建端口）与 [Module.scala:279-285](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L279-L285)（自动接线）这两处。若本地无法运行 firtool，请标注「待本地验证」。

#### 4.5.5 小练习与答案

**练习 1**：`Module` 的 `clock`/`reset` 是「普通的 `val`」还是「真正的端口」？依据是什么？

> **答案**：它们是真正的端口，由 `IO(Input(...))` 创建（[Module.scala:238-239](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L238-L239)）。`IO(...)` 内部调用 `_bindIoInPlace`，把信号以 `PortBinding` 绑定并加入 `_ports`。所以生成 Verilog 时它们和用户写的 `IO` 一样出现在端口表里。

**练习 2**：为什么 `RawModule` 适合做多时钟域设计？

> **答案**：`RawModule` 没有隐式 clock/reset，不会强制所有寄存器接同一个时钟。设计师可以用 `withClock(clkA)`、`withClock(clkB)` 在不同代码块里显式切换时钟域，模块端口也不会被强加 `clock`/`reset`。`Module` 的单一隐式时钟模型反而会碍事。

**练习 3**：`mkReset` 对顶层模块返回 `Bool()`，对非顶层返回 `Reset()`，为什么？

> **答案**：顶层模块的复位类型必须确定（否则综合器无从下手），故用具体的 `Bool()`（同步复位）；非顶层模块用未推断的 `Reset()`，把「同步还是异步」的决定权交给父模块/综合器，从而允许同一个模块在不同复位风格的环境下复用。可用 `RequireAsyncReset` 等强制覆盖（[RawModule.scala:236-243](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/RawModule.scala#L236-L243)）。

---

## 5. 综合实践

把本讲的知识串起来，完成下面这个「迷你 elaboration 侦探」任务。

**任务**：写一个有两级层次的模块，刻意制造一个**会在 `generateComponent` 阶段被发现**的错误（例如某个端口是局部变量而非 `val` 字段，导致 `checkPorts` 无法命名），运行 elaboration，然后根据报错信息回答：

1. 报错信息里有没有指向**用户代码**的文件名和行号？这个源信息是在哪一步被注入的？（提示：宏 `InstTransform` / `do_apply`。）
2. 报错是在 `evaluate` 内部、还是在 `generateComponent` 内部抛出的？依据源码哪一行？
3. 如果把同样的错误写进一个 `RawModule` 子类，报错是否相同？为什么？（提示：`Module` 继承自 `RawModule`，`checkPorts` 来自 `RawModule.generateComponent`。）

```scala
// 示例代码（非项目原有）——用一个会产生问题的写法，观察 checkPorts 的行为
import chisel3._
class Probe extends Module {
  val io = IO(new Bundle { val out = Output(Bool()) })
  io.out := 1.U
}
// 进阶：尝试把一个端口接在非 val 的临时变量上，或匿名 IO，观察命名报错
```

**交付物**：一段 200 字左右的「调查报告」，引用至少 3 处本讲提到的源码行号作为证据。

> 提示：`checkPorts` 在 [RawModule.scala:93-102](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/RawModule.scala#L93-L102)，它通过 `Builder.error` 收集错误而非立即抛出（错误收集机制见 u9-l2）；`nameId` 的端口分支在 [RawModule.scala:142-143](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/RawModule.scala#L142-L143)。

## 6. 本讲小结

- `Module(new MyMod)` 不是构造器调用，而是工厂包装：经宏 `do_apply` → `_applyImpl`，完成「定制 → 收口 → 上挂」三步。
- **定制**由 `Module.evaluate`（[Module.scala:65-108](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L65-L108)）负责：在 `Builder.State.guard` 的受控窗口里运行构造器，靠握手旗 `readyForModuleConstr` 保证恰好构造一个模块。
- **`BaseModule`**（[Module.scala:423](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L423)）是所有模块的抽象基类，其构造器完成与 Builder 的握手、设置 `currentModule`、压入命令块 `_body`，并持有 `_ids`/`_ports`/`_closed`/`_namespace` 等可变状态。
- **收口**由 `RawModule.generateComponent`（[RawModule.scala:158-196](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/RawModule.scala#L158-L196)）负责：命名所有 id、关闭模块、把命令块封装成 `DefModule` IR 组件。
- **`Module` 与 `RawModule` 的本质差别**仅在于隐式 clock/reset：`Module`（[Module.scala:232](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L232)）混入 `ImplicitClock`/`ImplicitReset`，自动创建 `clock`/`reset` 端口并在 `initializeInParent` 里接线；`RawModule` 不做这些，留给用户完全控制。
- 模块构造体里的每条 `IO`/`:=`/`when`/`Reg` 只是经 `addCommand` 把 `Command` 登记进当前 `Block`，真正的「固化成 IR」发生在 `generateComponent` 收口那一刻——这是「只登记不施工」的精确含义。

## 7. 下一步学习建议

本讲把「一个模块如何被构造、收口、实例化」讲清了，但还有几条线没收：

- **命令是如何被记录成 IR 节点的**：`addCommand` 放进去的 `Command` 到底有哪些种类（`DefNode`/`Connect`/`DefReg`...）？`DefModule` 又是如何汇成顶层 `Circuit` 的？→ 下一单元的 **u4-l1（Builder 全局状态机）** 与 **u4-l2（命令记录与内部 FIRRTL IR）**。
- **连线操作符 `:=`/`<>` 的方向检查**：本讲提到 `clock := forcedClock` 这类连线，但连线的方向合法性由谁检查？→ **u3-l4（MonoConnect 与 BiConnect）**。
- **隐式 clock/reset 的实际用法**：`withClock`/`withReset` 如何临时切换隐式域？→ **u3-l5（when 与 Reg）** 会用到，多时钟域设计可参考 `MultiClock.scala`。
- **绑定（Binding）系统**：`nameId` 里反复出现的 `PortBinding`/`RegBinding`/`WireBinding`/`OpBinding` 到底是什么？→ **u4-l3（Binding 系统）**。
- **命名与去重**：`_forceName`、`Namespace` 如何避免信号名冲突？→ **u7-l3（Namer 与 Identifier）**。

建议先做 u4-l1、u4-l2，把「命令 → IR → Circuit」的链路补全，再回头看你今天写的 `RegModuleA` 生成的 Verilog，会有「原来如此」的顿悟。
