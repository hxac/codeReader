# Layers 与 Probe

## 1. 本讲目标

本讲讲解 Chisel 两个面向「验证 / 调试 / 可观测性」的现代核心特性：

- **Layer（层）**：把一段硬件代码（断言、打印、监视器等）隔离进一个「可选编译层」，在 Verilog 编译期才决定是否启用，从而既能在仿真时打开、又能在交付时整体剥离。
- **Probe（探针）**：以「引用」的方式把内部信号暴露出去（最终落地为 SystemVerilog 层次名），不占用普通硬件端口，常用于构建验证接口；探针还可与层结合，成为「层着色的探针」。

学完后你应当能够：

1. 说清 Layer 与传统 `when` 条件块的本质区别（编译期可选 vs. 运行期选择）。
2. 会声明用户自定义 Layer，会用 `layer.block` 把代码塞进层，能看懂生成的 CHIRRTL/SystemVerilog 里层是如何被隔离的。
3. 知道 `chisel3.layers` 提供的内建层（`Verification`/`Assert`/`Assume`/`Cover`/`Debug`/`Temporal`），以及为什么一句普通 `printf` 会自动落进 `Verification.Debug`。
4. 掌握 `Probe`/`ProbeValue`（只读）、`RWProbe`/`RWProbeValue`（读写）、`define`、`read` 这套探针 API，能把内部信号以探针形式引出。

## 2. 前置知识

阅读本讲前，你应当已经掌握：

- **模块与 elaboration 生命周期**（见 u3-l1）：模块构造体里的代码「只登记不施工」，每条语句最终经 `Builder.pushCommand` 落成内部 FIRRTL 命令。
- **命令与内部 IR**（见 u4-l2）：`Connect`、`When`、`DefPrim` 等都是挂在模块 `Block` 上的 `Command` 节点；本讲的 `LayerBlock`、`ProbeDefine` 也是同类命令。
- **Stage / Phase 管道**（见 u5-l1）：最终 SystemVerilog 由 firtool（CIRCT）产出，Layer 与 Probe 的「隔离 / 层次名」都在这一段落地。

两个对理解本讲至关重要的术语：

- **SystemVerilog `bind`（IEEE 1800-2023 §23.11）**：把一个模块的实例「绑定」进另一个模块的作用域，是 Extract 层在 Verilog 侧的落地手段。
- **SystemVerilog 层次名（hierarchical name）**：形如 `top.inst.sig` 的跨模块引用，是 Probe 在 Verilog 侧的落地手段。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [core/src/main/scala/chisel3/Layer.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Layer.scala) | `layer` 包对象：`Layer` 抽象类、`LayerConfig`、`block`/`enable`/`addLayer`/`elideBlocks` 等 API，是层的「引擎」。 |
| [core/src/main/scala/chisel3/layers/Layers.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/layers/Layers.scala) | 内建层：`Verification` 及其子层 `Assert`/`Assume`/`Cover`/`Debug`、以及 `Temporal`。 |
| [core/src/main/scala/chisel3/layers/package.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/layers/package.scala) | `defaultLayers` 列表：决定哪些层「无条件」出现在输出里。 |
| [core/src/main/scala/chisel3/probe/package.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/probe/package.scala) | `probe` 包对象：`define`、`read`、`force`/`release` 等探针操作。 |
| [core/src/main/scala/chisel3/probe/ProbeBase.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/probe/ProbeBase.scala) | `Probe`/`RWProbe` 共用的构造逻辑：给类型打上「探针修饰符」。 |
| [core/src/main/scala/chisel3/probe/ProbeValueBase.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/probe/ProbeValueBase.scala) | `ProbeValue`/`RWProbeValue`：把一个硬件值包装成探针「表达式」。 |
| [core/src/main/scala-2/chisel3/probe/Probe.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala-2/chisel3/probe/Probe.scala) | `object Probe` / `object RWProbe`：用户入口（Scala 2 版，带宏）。 |
| [core/src/main/scala/chisel3/Data.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala) | `ProbeInfo`：每个 `Data` 身上记录「是不是探针、是否可写、层颜色」。 |
| [core/src/main/scala/chisel3/SimLog.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/SimLog.scala) | 把 `printf` 自动塞进 `Verification.Debug` 层的实现。 |

## 4. 核心概念与源码讲解

### 4.1 Layer 机制：把代码隔离到可剥离的编译层

#### 4.1.1 概念说明

Layer 描述的是「一段可选的硬件功能」。它与 `when` 条件块有根本区别：

- `when(cond){...}` 是**运行期**选择：两段硬件都被综合出来，靠 mux 在每个时钟沿挑选。
- `layer.block(L){...}` 是**编译期**可选：整段代码被搬到一个独立的层里，只有当用户在 Verilog 编译期「启用」该层时，这段代码才会出现在电路中；否则它被完全剥离，连一根线都不剩。

典型用途：断言（assert）、覆盖率（cover）、执行轨迹打印（printf）、监视器——这些代码仿真时想要、交付芯片时想删，用层正好。

一个层由两件东西组成：

1. **层声明（declaration）**：一个继承 `chisel3.layer.Layer` 的单例 `object`，声明「这个可选功能可以存在」。
2. **层块（layer block）**：模块体内用 `layer.block(L){...}` 包起来的一段代码，即「可选功能本身」。

层有两种「下放到 Verilog 的约定（convention）」，由 `LayerConfig` 区分：

- **Extract 层**：层块被搬进单独的 Verilog 模块，用 SystemVerilog `bind` 挂回原模块；通过「在编译时 `include` 一个约定名字的文件」来启用。
- **Inline 层**：层块留在原地，但用 `` `ifdef 宏 `` 包起来；通过「在编译时定义一个约定名字的宏」来启用。

层可以嵌套，形成父子树。子层只能访问父层已启用的内容，即「子层启用 ⇒ 父层必启用」。

#### 4.1.2 核心流程

声明一个 Extract 层并使用层块的端到端流程：

```
1. object MyLayer extends Layer(LayerConfig.Extract())   // 声明层
2. 模块体内：layer.block(MyLayer) { ...代码... }          // 创建层块
3. layer.block 内部：
   a. 沿父链补齐要创建的层（layersToCreate）
   b. addLayer(_) 把层登记进 Builder.layers（决定是否发射）
   c. 递归 pushCommand(new LayerBlock(...))，切换 Block 上下文
   d. 在最内层 Block 里执行用户的 thunk → 命令落入层块
4. elaboration 收口：层块作为 Command 节点进入内部 IR
5. 下游 firtool：Extract 层 → 独立模块 + bind 文件；Inline 层 → `ifdef 宏
```

层之间的父子关系形成一个偏序，探针的「层着色」读写权限就建立在这个偏序上（见 4.4）。

#### 4.1.3 源码精读

`Layer` 抽象类持有一个 `LayerConfig` 和一个隐式的父层 `_parent`，靠 `protected final implicit val thiz` 把自己作为嵌套子层的隐式父层——这正是「在 `object` 里再定义 `object` 就能嵌套」的原理：

[core/src/main/scala/chisel3/Layer.scala:91-106](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Layer.scala#L91-L106) — `abstract class Layer` 构造接收 `config` 与隐式 `_parent`；第 106 行建立 `thiz` 隐式值供子层继承父层。

`LayerConfig` 是一个密封 trait，三个取值分别对应 Extract（带输出目录行为）、Inline、以及内部的 Root：

[core/src/main/scala/chisel3/Layer.scala:70-83](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Layer.scala#L70-L83) — `Extract` 的 ABI 是 `FileInclude`（include 文件启用），`Inline` 的 ABI 是 `PreprocessorDefine`（定义宏启用）。

层的「全名」由父链拼出（如 `A.B`），`canWriteTo` 用递归判断祖先关系——这是探针层着色权限的判定基础：

[core/src/main/scala/chisel3/Layer.scala:158-163](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Layer.scala#L158-L163) — `canWriteTo(that)`：沿 `parent` 链向上找，若 `that` 是本层（含自身）的祖先则可写。

`layer.block` 是创建层块的核心 API。它先决定「要不要创建」（几个 skip 开关），再沿父链算出要补齐哪些层，最后递归 `pushCommand(new LayerBlock(...))` 并切换 Block 上下文执行用户代码：

[core/src/main/scala/chisel3/Layer.scala:343-374](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Layer.scala#L343-L374) — `block` 签名（带 `skipIfAlreadyInBlock`/`skipIfLayersEnabled` 两个开关）与 `layersToCreate` 的计算：从目标层沿 `parent` 回溯到当前层栈顶，沿途收集要创建的层；空则直接 `tc.identity(thunk)` 不建块。第 374 行 `addLayer(_layer)` 把层登记进 Builder。

[core/src/main/scala/chisel3/Layer.scala:390-401](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Layer.scala#L390-L401) — `createLayers` 递归：每深入一层就 `pushCommand(new LayerBlock(...))`、把该层压入 `Builder.layerStack`、用 `withRegion(layerBlock.region)` 切换命令落点，最内层执行 `thunk`。

层块在内部 IR 里就是一个 `LayerBlock` 命令，它持有一个 `region: Block` 容器装这一层的命令：

[core/src/main/scala/chisel3/internal/firrtl/IR.scala:514-520](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala#L514-L520) — `class LayerBlock` 持 `layer` 与 `region`，`region` 是一个 `Block`，层块内的所有命令都挂在它下面。

`addLayer` 负责把层（连同其所有父层）登记进 `Builder.layers`，这决定了该层是否会被发射进 FIRRTL 文本：

[core/src/main/scala/chisel3/Layer.scala:191-206](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Layer.scala#L191-L206) — 沿父链 while 循环，把还没登记的层加入 `Builder.layers`。

`Builder` 在 `DynamicContext` 里维护层相关的全局状态：`layers`（已登记层集合）、`layerStack`（当前层块栈，栈顶即最内层层块）、`enabledLayers`（被 `enable` 打开的层）、`elideLayerBlocks`（是否抑制建块）：

[core/src/main/scala/chisel3/internal/Builder.scala:535-554](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L535-L554) — `layers`/`enabledLayers`/`layerStack` 的可变状态定义。

#### 4.1.4 代码实践

**实践目标**：亲手声明一个用户自定义 Extract 层，用 `layer.block` 放一段代码进去，对照生成的 CHIRRTL 观察层块如何把代码隔离出来。

**操作步骤**：

1. 准备如下模块（示例代码，可用 `scala-cli` 或在本项目测试里运行）：

```scala
// 示例代码
import chisel3._
import chisel3.layer.{Layer, LayerConfig}

// 1) 声明一个用户自定义 Extract 层
object MyLayer extends Layer(LayerConfig.Extract())

class Foo extends RawModule {
  val a = IO(Input(Bool()))

  // 2) 用 layer.block 把一段代码隔离进 MyLayer
  chisel3.layer.block(MyLayer) {
    val w = WireInit(a)   // 这个 wire 只在 MyLayer 启用时才存在
  }

  // 模块本体里也有一个 wire，作对照
  val y = Wire(Bool())
  y := DontCare
}
```

2. 用 `emitCHIRRTL` 打印内部 IR（这一步不调用 firtool，能直接看到层块结构）：

```scala
// 示例代码
import circt.stage.ChiselStage
println(ChiselStage.emitCHIRRTL(new Foo))
```

**需要观察的现象**：

- CHIRRTL 里会先出现层声明 `layer MyLayer, bind, "MyLayer" :`。
- 模块 `Foo` 体内会出现 `layerblock MyLayer :`，而 `wire w` 缩进在它下面；对照之下 `wire y` 直接挂在模块顶层，不在任何层块里。

**预期结果**（CHIRRTL 片段，待本地验证确切格式）：

```
layer MyLayer, bind, "MyLayer" :
module Foo :
  ...
  layerblock MyLayer :
    wire w : UInt<1>
  wire y : UInt<1>
```

5. **若进一步生成 SystemVerilog**（`emitSystemVerilog(new Foo, firtoolOpts = Array("-enable-layers=MyLayer"))`），可观察到 Extract 层会把层块搬进单独的模块（名字类似 `Foo_MyLayer`，但**模块名不属于 ABI、不可依赖**），并通过一个约定名字的 bind 文件（如 `layers-Foo-MyLayer.sv`）挂回 `Foo`。这部分细节由 firtool 实现，具体输出待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：把上面的 `MyLayer` 从 `LayerConfig.Extract()` 改成 `LayerConfig.Inline`，重新生成 CHIRRTL，层声明那一行会有什么变化？生成的 SystemVerilog 启用方式又有什么不同？

> **参考答案**：CHIRRTL 的层声明会从 `bind`（对应 Extract）变成 `` `ifdef `` 风格的 inline 标记；SystemVerilog 侧不再是 bind 文件，而是用一个以 `layer$` 开头、层名以 `$` 分隔的预处理宏（如 `layer$MyLayer`）来守护层块代码，定义该宏即启用。

**练习 2**：`layer.block` 的 `skipIfAlreadyInBlock = true` 参数有什么用？

> **参考答案**：若调用点已经身处某个层块内部，设为 `true` 就**不再新建**层块（直接以 `identity` 返回），常用于「库作者自动把代码塞进层、但允许用户在已处层块时跳过」的场景；若为 `false`，则会在已有层块里再嵌套一层（层允许在自身下嵌套自身，因为是「祖先」而非「真祖先」关系）。

### 4.2 内建 layers：Verification / Assert / Debug 等

#### 4.2.1 概念说明

Chisel 在 `chisel3.layers` 包里预置了一组「内建层」，覆盖最常见的验证场景，避免每个项目各自发明一套层约定：

```
chisel3.layers.Verification                          (Extract)
├── chisel3.layers.Verification.Assert               (Extract)
│   └── chisel3.layers.Verification.Assert.Temporal  (Inline)
├── chisel3.layers.Verification.Assume               (Extract)
│   └── chisel3.layers.Verification.Assume.Temporal  (Inline)
├── chisel3.layers.Verification.Cover                (Extract)
│   └── chisel3.layers.Verification.Cover.Temporal   (Inline)
└── chisel3.layers.Verification.Debug                (Extract)
```

其中 `Assert`/`Assume`/`Cover` 各自带一个 `Temporal` 内联子层，用来单独安放「时序属性」——因为有些仿真器对 SystemVerilog 时序断言支持不全，把它们隔离进可单独关闭的层能绕开限制。

更重要的是：Chisel 标准库的若干 API **会自动把代码放进这些层**，用户无需手写 `layer.block`：

| 操作 | 自动落入的层 |
| --- | --- |
| `chisel3.assert` | `Verification.Assert` |
| `chisel3.assume` | `Verification.Assume` |
| `chisel3.cover` | `Verification.Cover` |
| `printf` | `Verification.Debug` |

> 注：规格里提到的「block/test 等」是对内建层的概括说法。源码中并不存在名为 `test` 的层；与测试/验证相关的内建层就是上面这组 `Verification.*`。`block` 则是 4.1 介绍过的 `layer.block` 方法。

#### 4.2.2 核心流程

内建层本身就是普通的 `Layer` 子类，只是它们的声明被预先写好、并被列入 `defaultLayers`：

```
1. 内建层 object（如 Verification.Debug）在 chisel3.layers 包中被定义
2. layers.defaultLayers 把它们列入「永远出现」清单
3. 标准 API（如 printf）内部调用 layer.block(Verification.Debug, skipIfAlreadyInBlock=true, skipIfLayersEnabled=true)
4. 用户写的 printf 命令因此自动进入 Debug 层块
5. 输出永远包含这些层的 bind 文件（即便没人用，ABI 也要求产出空文件）
```

#### 4.2.3 源码精读

`Verification` 是一个 Extract 层，其输出目录被指定为 `verification`；`Assert`/`Assume`/`Cover`/`Debug` 都是它的子层，各自指定子目录，并（前三个）混入 `HasTemporalInlineLayer` 获得 `Temporal` 内联子层：

[core/src/main/scala/chisel3/layers/Layers.scala:32-71](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/layers/Layers.scala#L32-L71) — `object Verification extends Layer(LayerConfig.Extract(CustomOutputDir(...)))`，内部嵌套定义 `Assert`/`Assume`/`Cover`/`Debug`，前三者 `with HasTemporalInlineLayer`。

[core/src/main/scala/chisel3/layers/Layers.scala:19-29](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/layers/Layers.scala#L19-L29) — `HasTemporalInlineLayer` trait：混入任意用户层即可获得一个 `Temporal` 内联子层。

`defaultLayers` 把全部内建层（含三个 `Temporal`）列成「永远出现」清单，保证输出可预测：

[core/src/main/scala/chisel3/layers/package.scala:13-22](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/layers/package.scala#L13-L22) — `defaultLayers` 序列。

最关键的「自动入层」机制在 `printf`：用户调用 `printf(...)` 最终走 `SimLog.StdErr.printf`，而它在登记 `Printf` 命令前，先用 `layer.block(Verification.Debug, ...)` 把自己包起来——这就是「一句普通 printf 自动落进 Debug 层」的全部秘密（对应近期提交「Add Debug Layer, put printfs in them」）：

[core/src/main/scala/chisel3/Printf.scala:30-31](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Printf.scala#L30-L31) — `printf.apply` 直接转调 `SimLog.StdErr.printf(pable)`。

[core/src/main/scala/chisel3/SimLog.scala:84-86](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/SimLog.scala#L84-L86) — `printfWithoutReset` 用 `layer.block(layers.Verification.Debug, skipIfAlreadyInBlock = true, skipIfLayersEnabled = true)` 包裹 `Printf` 命令的登记。

#### 4.2.4 代码实践

**实践目标**：验证一句普通 `printf` 确实自动进入了 `Verification.Debug` 层，而无需手写 `layer.block`。

**操作步骤**：

```scala
// 示例代码
import chisel3._
import circt.stage.ChiselStage

class P extends Module {
  val in = IO(Input(UInt(8.W)))
  val sum = RegInit(0.U(8.W))
  sum := sum + in
  printf("sum = %d\n", sum)   // 没有手写 layer.block
}

println(ChiselStage.emitCHIRRTL(new P))
```

**需要观察的现象 / 预期结果**：CHIRRTL 中，`printf` 不会直接挂在模块顶层，而是出现在 `layerblock Verification :` →（`when` 复位门）→ `layerblock Debug :` 的嵌套结构里（printf 默认带复位门，由 `when(!reset)` 包裹；确切嵌套层数待本地验证）。这说明 `printf` 被 `SimLog` 自动塞进了 Debug 层。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `printf` 自动进入的是 `Verification.Debug` 而不是 `Verification.Assert`？

> **参考答案**：因为 `printf` 的实现（`SimLog.printfWithoutReset`）硬编码调用了 `layer.block(layers.Verification.Debug, ...)`。打印属于「调试/日志」语义，与断言（`Assert`）分属不同关注点，故各归各位。

**练习 2**：`skipIfLayersEnabled = true` 在 `printf` 的入层调用里起什么作用？

> **参考答案**：当模块已经用 `layer.enable(...)` 打开了某些层（典型是测试平台想一次性读到所有探针）时，`skipIfLayersEnabled=true` 会让 `layer.block` 不再新建 Debug 层块，而是把 printf 直接留在当前（已启用层的）上下文里，避免无谓的嵌套。

### 4.3 Probe / ProbeValue：可综合的探针

#### 4.3.1 概念说明

Probe（探针）是对一块硬件的「引用」。它最终落地为 SystemVerilog 的层次名（如 `top.dut.reg_q`），用来在不增加普通端口的前提下，把内部信号暴露给断言、监视器或测试平台。

理解探针的两个关键反直觉点：

1. **探针不是「影子数据流」**。它不是一条额外的连线，而是一个「按名字解析」的引用；引用在访问点必须能无歧义地解析到被探的值。
2. **探针类型是合法的端口 / 线网类型，但不是状态元件类型**（不能做 `Reg`/`Mem` 的元素）。线网（`Wire`）在 Chisel 里更像「变量」，所以可以承载一个引用。

探针分两种：

| 种类 | 类型构造 | 值构造 | 语义 |
| --- | --- | --- | --- |
| 只读探针 | `Probe(T)` | `ProbeValue(x)` | 只能 `read`，被动观测 |
| 读写探针 | `RWProbe(T)` | `RWProbeValue(x)` | 还能 `force`/`release`，主动注入（故障注入等） |

与普通硬件的「最后连接语义（last-connect）」不同，探针只能被 **`define` 恰好一次**——它更像「一次性转发一个引用」。

#### 4.3.2 核心流程

把内部信号以只读探针引出到端口的流程：

```
1. 端口声明：val p = IO(Output(Probe(UInt(32.W))))     // 探针「类型」
2. 取探针值：val pv = ProbeValue(internalSignal)        // 探针「表达式」
3. 定义探针：probe.define(p, pv)                         // 把引用转发到端口
4. 下游 firtool：端口被替换成层次名（ref_<module>.sv 里给出宏定义）
```

`Probe(T)` 给类型 `T` 打上一个「探针修饰符」（记录在 `Data` 的 `ProbeInfo` 里），`ProbeValue(x)` 则把一个已存在的硬件值 `x` 包装成探针表达式（`ProbeExpr`），`define` 把两者绑定为一条 `ProbeDefine` 命令。

#### 4.3.3 源码精读

每个 `Data` 都有一个可选的 `ProbeInfo`，记录「是否探针、是否可写、层颜色」：

[core/src/main/scala/chisel3/Data.scala:373-376](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L373-L376) — `_probeInfoVar` 私有变量与 `probeInfo` 访问器；`probeInfo` 为 `None` 即普通类型，非 `None` 即探针类型。

[core/src/main/scala/chisel3/Data.scala:900](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L900) — `case class ProbeInfo(writable: Boolean, color: Option[layer.Layer])`：`writable` 区分只读/读写，`color` 即层颜色（见 4.4）。

`Probe`/`RWProbe` 共用 `ProbeBase.apply`：它先用 `Output(source)` 把类型强制成被动方向，做合法性检查（不能探针套探针、不能探针含探针的聚合、不能对 const 做读写探针），再给结果打上 `ProbeInfo`：

[core/src/main/scala/chisel3/probe/ProbeBase.scala:15-49](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/probe/ProbeBase.scala#L15-L49) — 第 24 行 `Output(source)` 强制被动；第 25-31 行合法性检查；第 37-47 行处理层颜色（若给了 `_color` 则 `layer.addLayer` 登记该层）并 `setProbeModifier`。

用户入口 `object Probe` / `object RWProbe` 是带宏的薄封装，`do_apply` 分别以 `writable=false/true` 调用 `super.apply`：

[core/src/main/scala-2/chisel3/probe/Probe.scala:10-28](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala-2/chisel3/probe/Probe.scala#L10-L28) — `object Probe extends ProbeBase`，`do_apply(source)` 传 `false`（只读）；带 `color` 的重载用于层着色（见 4.4）。

`ProbeValue` 把硬件值包成探针表达式：克隆一份探针类型、绑成 `OpBinding`、把引用设为 `ProbeExpr(source.ref)`（读写版用 `RWProbeExpr`，并拒绝字面量）：

[core/src/main/scala/chisel3/probe/ProbeValueBase.scala:12-32](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/probe/ProbeValueBase.scala#L12-L32) — 第 15 行按 `writable` 选 `Probe`/`RWProbe`；第 23-29 行只读版对字面量先转成一个中间 `Wire` 再探针。

对应的内部 IR 节点：

[core/src/main/scala/chisel3/internal/firrtl/IR.scala:291-293](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala#L291-L293) — `ProbeExpr`/`RWProbeExpr`/`ProbeRead` 三个 `Arg`，分别表达「取只读探针值」「取读写探针值」「读探针」。

#### 4.3.4 代码实践

**实践目标**：把一个内部寄存器以只读探针的形式引出到模块端口，并用 `emitSystemVerilog` 观察端口如何变成层次名引用。

**操作步骤**：

```scala
// 示例代码
import chisel3._
import chisel3.probe.{Probe, ProbeValue, define}
import circt.stage.ChiselStage

class Foo extends Module {
  val d = IO(Input(UInt(32.W)))
  val q = IO(Output(UInt(32.W)))
  // 探针端口：注意它不参与普通数据流，是「引用」
  val r_probe = IO(Output(Probe(UInt(32.W))))

  private val r = Reg(UInt(32.W))
  r := d
  q := r

  // 把内部寄存器 r 以探针形式转发到端口
  define(r_probe, ProbeValue(r))
}

println(ChiselStage.emitSystemVerilog(
  new Foo,
  firtoolOpts = Array("-strip-debug-info", "-disable-all-randomization")
))
```

**需要观察的现象**：

- `Foo` 模块的端口列表里，`r_probe` **不会**变成一根普通的 input/output 线，而是与一个层次名宏（`ref_Foo_r_probe`）相关联；最终会产出一个 `ref_Foo.sv` 文件，里面用文本宏定义了这个引用。

**预期结果**：生成的 SystemVerilog 中，对 `r_probe` 的使用体现为对 `Foo.r`（被探信号）的层次引用。确切文本由 firtool 决定，待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么探针不能作为 `Reg` 或 `Mem` 的元素类型？

> **参考答案**：探针是「引用」而非「硬件连线/存储」，状态元件（寄存器、内存）存的是值而非引用。让 `Reg(Probe(...))` 在语义上说不通（一个寄存器里「存一个指向别处的名字」没有硬件对应物），故被禁止；探针只允许出现在端口和线网上。

**练习 2**：`ProbeValue(5.U)`（直接对字面量取探针值）会发生什么？

> **参考答案**：只读版 `ProbeValueBase.apply` 检测到 `source.isLit` 时，会先生成一个中间 `Wire`（`suggestName("lit_probe_val")`）把字面量接住，再对这个 wire 取探针；而读写版 `RWProbeValue` 对字面量直接 `Builder.error("Cannot get a probe value from a literal.")`，因为读写探针要能 `force`，字面量无法被改写。

### 4.4 define / read 与层着色探针

#### 4.4.1 概念说明

探针有三类核心操作：

- `probe.define(sink, source)`：把一个探针表达式 `source`「定义」到探针 `sink` 上（一次性，类似单次连接）。也可用标准连接算子 `:<=` 隐式完成。
- `probe.read(p)`：把探针 `p` 读回成普通硬件值，参与正常运算。
- 对读写探针：`force`/`forceInitial` 覆盖被探信号的值，`release`/`releaseInitial` 解除覆盖（落地为 SystemVerilog `force`/`release`）。

**层着色（layer-coloring）** 是探针与层的结合：声明探针时可带一个「颜色」（一个层），表示「这个探针仅当该层启用时才存在」：

```scala
val a = IO(Output(Probe(Bool(), MyLayer)))   // 带颜色 MyLayer
```

层着色的读写权限遵循偏序规则（记住一句口诀）：

> 可向「本层或子层」写入；可从「本层或父层」读取。

形式化地，设探针颜色为 \(c_p\)、当前代码所在层为 \(c\)，则：

- 允许 `define`（写）当且仅当 \(c\) 可写向 \(c_p\)，即 \(c_p\) 是 \(c\) 的祖先或相等：\(c.\text{canWriteTo}(c_p)\)。
- 允许 `read`（读）当且仅当 \(c_p\) 可写向 \(c\)，即 \(c\) 是 \(c_p\) 的祖先或相等。

这保证了「可选代码不会影响必选代码」：层块（可选）只能往外写与自己同色或更「可选」的探针，绝不能改写主设计的信号。

#### 4.4.2 核心流程

```
define(sink, source):
  1. reify sink（拆封 identity view），校验可写
  2. 类型/位宽等价检查（strictProbeInfo=false，下面再细查颜色）
  3. 要求 sink 是探针、且是探针根（不是探针的子字段）
  4. requireCompatibleDestinationProbeColor：用 Builder.layerStack 检查颜色偏序
  5. 若 sink 可写，还要求 source 也是可写探针
  6. pushCommand(ProbeDefine(sink.lref, source.ref))
```

`read` 则构造一个克隆，绑成 `OpBinding`，引用设为 `ProbeRead(source.ref)`，并清除结果身上的探针修饰符，使其能参与普通连线。

#### 4.4.3 源码精读

`probe.define` 是探针的「单次初始化」，内含层层校验，最后压入 `ProbeDefine` 命令：

[core/src/main/scala/chisel3/probe/package.scala:35-66](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/probe/package.scala#L35-L66) — 第 36-40 行拆封 sink 并校验可写；第 41-49 行类型等价检查；第 50-52 行要求 sink/source 都是探针；第 53-58 行 `requireCompatibleDestinationProbeColor` 用当前层栈检查「当前层能否写向该探针颜色」；第 59-64 行读写探针的额外要求；第 65 行 `pushCommand(ProbeDefine(...))`。

`probe.read`（来自 `ProbeObjIntf`）的宏入口与实现：

[core/src/main/scala-2/chisel3/probe/PackageIntf.scala:16-19](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala-2/chisel3/probe/PackageIntf.scala#L16-L19) — `def read` 是宏，`do_read` 转调 `probe._readImpl`。

[core/src/main/scala/chisel3/probe/package.scala:68-78](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/probe/package.scala#L68-L78) — `_readImpl`：克隆源类型、绑 `OpBinding`、`setRef(ProbeRead(source.ref))`、`clearProbeInfo` 后返回普通硬件值。

读写探针的 `force`/`release` 落地为带时钟与条件的 `ProbeForce`/`ProbeRelease` 命令（条件默认取自 `Module.disableOption`，即通过 `Disable` API 的复位门）：

[core/src/main/scala/chisel3/probe/package.scala:140-161](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/probe/package.scala#L140-L161) — `force`/`release` 都先 `requireHasWritableProbeTypeModifier`，再取 `forcedClock` 与 disable 条件，`pushCommand(ProbeForce/ProbeRelease(...))`。

对应的内部 IR 命令族：

[core/src/main/scala/chisel3/internal/firrtl/IR.scala:547-551](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala#L547-L551) — `ProbeDefine`/`ProbeForceInitial`/`ProbeReleaseInitial`/`ProbeForce`/`ProbeRelease` 五条探针命令。

层着色探针的颜色，正是通过 `Probe(T, color)` 的第二个参数注入，最终存进 `ProbeInfo.color`（见 4.3.3 的 `ProbeBase` 第 37-47 行）；`define` 再用 `canWriteTo` 做偏序校验（见 4.1.3 的 `canWriteTo`）。

#### 4.4.4 代码实践

**实践目标**：在一个用户自定义层块内部，用一个层着色探针把块内算出的值「送」到模块顶层端口；体会「层块代码不影响主设计、只能经同色探针向外通信」。

**操作步骤**：

```scala
// 示例代码
import chisel3._
import chisel3.layer.{Layer, LayerConfig}
import chisel3.probe.{Probe, ProbeValue, define}
import circt.stage.ChiselStage

object A extends Layer(LayerConfig.Extract())

class Foo extends RawModule {
  val port = IO(Input(Bool()))
  // 层着色探针端口：颜色 = A，仅当 A 启用时存在
  val a = IO(Output(Probe(Bool(), A)))

  // 层块内的值，经同色探针送出
  chisel3.layer.block(A) {
    val a_wire = WireInit(false.B)
    define(a, ProbeValue(a_wire))
  }
}

println(ChiselStage.emitCHIRRTL(new Foo))
```

**需要观察的现象**：

- CHIRRTL 中，`define` 出现在 `layerblock A :` 内部；探针端口 `a` 的类型带颜色 `A`。
- 主模块 `Foo` 本体里没有任何由层块引入的逻辑——层块被隔离，且只通过探针 `a` 与外界联系。

**预期结果**：层块内的 `a_wire` 与 `define` 都位于 `layerblock A` 下；若不启用层 A，这些代码在最终 SystemVerilog 中不存在，`a` 端口也随之消失。确切文本待本地验证。

> **关于规格中的 `probe.define`**：它就是 `chisel3.probe.define`（本节源码精读的对象）。`ProbeValue` 把内部信号包成探针表达式，`define` 把它一次性转发到探针端口——这正是「把内部信号引出到探针」的标准写法。

#### 4.4.5 小练习与答案

**练习 1**：若把上例中探针端口的颜色改成另一个与 `A` 毫无父子关系的层 `B`，`define` 会怎样？

> **参考答案**：`requireCompatibleDestinationProbeColor` 会用当前层栈（栈顶是 `A`）检查 `A.canWriteTo(B)`。由于 `B` 既不是 `A` 也不是 `A` 的祖先，校验失败，`Builder.error` 报出「无法从颜色 A 的层块定义颜色 B 的探针」之类错误。

**练习 2**：`define` 和普通 `:=`/`:<=` 连接有什么本质区别？

> **参考答案**：普通连接遵循「最后连接语义」，一个信号可被连多次、最后一次生效；`define` 是「恰好一次」的探针初始化，重复 define 会冲突。不过为了方便，标准连接算子（如 `:<=`）作用于探针时会自动转成 `define`，所以用户层面写连接也能工作，但底层语义仍是单次定义。

## 5. 综合实践

把本讲全部要点串起来：写一个带累加器的模块，**同时**演示（a）printf 自动入 Debug 层、（b）用户自定义层隔离一段可选逻辑、（c）把内部信号以探针引出。

```scala
// 示例代码
import chisel3._
import chisel3.layer.{Layer, LayerConfig}
import chisel3.probe.{Probe, ProbeValue, define}
import circt.stage.ChiselStage

object MyLayer extends Layer(LayerConfig.Extract())

class Foo extends Module {
  val in  = IO(Input(UInt(32.W)))
  val out = IO(Output(UInt(32.W)))
  // (c) 内部累加值以只读探针引出，不占普通端口语义
  val accProbe = IO(Output(Probe(UInt(32.W))))

  private val acc = RegInit(0.U(32.W))
  acc := acc + in
  out := acc

  // (a) printf 自动落入 Verification.Debug 层（无需手写 layer.block）
  printf("acc = %d\n", acc)

  // (b) 用户自定义层：隔离一段可选逻辑
  chisel3.layer.block(MyLayer) {
    val monitor = WireInit(acc)   // 仅在 MyLayer 启用时存在
  }

  // (c) define 把内部 acc 引出到探针端口
  define(accProbe, ProbeValue(acc))
}
```

请依次完成：

1. 运行 `ChiselStage.emitCHIRRTL(new Foo)`，在输出里分别标出：`printf` 所在的 `layerblock ... Debug`、`monitor` 所在的 `layerblock MyLayer`、以及 `define`/`ProbeValue` 对应的 `probe`/`define` 节点。
2. 运行 `ChiselStage.emitSystemVerilog(new Foo, firtoolOpts = Array("-disable-all-randomization", "-enable-layers=Verification,Verification.Debug,MyLayer"))`，观察是否出现了独立的层模块（如 `Foo_Verification_Debug`、`Foo_MyLayer`）以及 bind 文件命名（`layers-Foo-*.sv`）；并确认 `accProbe` 端口以层次名引用的形式出现、产出了 `ref_Foo.sv`。
3. 把 `-enable-layers=MyLayer` 去掉再生成一次，对比 `Foo_MyLayer` 相关代码是否被整体剥离——直观感受「编译期可选」与 `when` 的「运行期选择」之别。

> 提示：层模块名、bind 文件名之外的细节都由 firtool 决定，可能随版本变化；只有 bind 文件名（`layers-<电路>-<层路径>.sv`）与宏名（`layer$<层路径>`）属于 ABI，可稳定依赖。若环境未装 firtool，CHIRRTL 部分仍可独立验证，SystemVerilog 部分标注「待本地验证」。

## 6. 本讲小结

- **Layer 是编译期可选、`when` 是运行期选择**：层块整段代码在 Verilog 编译期才决定是否存在，仿真可开、交付可删，专用于断言/打印/监视等验证代码。
- 层有 Extract（`bind` + include 文件）与 Inline（`` `ifdef `` 宏）两种约定，由 `LayerConfig` 区分；层可嵌套成树，子层启用必带父层启用。
- `layer.block(L){...}` 是建层块入口，内部沿父链补齐层、`addLayer` 登记、`pushCommand(new LayerBlock(...))` 切换 Block 上下文；层块在内部 IR 里就是一个带 `region: Block` 的 `LayerBlock` 命令。
- `chisel3.layers` 预置 `Verification`/`Assert`/`Assume`/`Cover`/`Debug`/`Temporal` 内建层；标准 API 会自动入层——尤其 **`printf` 经 `SimLog` 自动落入 `Verification.Debug`**。
- **Probe 是对硬件的「引用」**，落地为 SystemVerilog 层次名，不占普通端口；`Probe`/`ProbeValue` 只读、`RWProbe`/`RWProbeValue` 读写；探针只能 `define` 恰好一次，用 `read` 读回普通值，读写探针用 `force`/`release`。
- **层着色探针**遵循偏序：可向本层/子层写、可从本层/父层读，由 `ProbeInfo.color` + `Layer.canWriteTo` + `define` 的颜色校验共同保证「可选代码不影响主设计」。

## 7. 下一步学习建议

- **u8-l1（Definition/Instance）**：层级化设计探针常与 `Definition`/`Instance` 配合，跨层暴露 `@public` 字段。
- **u9-l1（测试体系）**：探针端口是 ChiselSim/ChiselTest 读取内部状态的主要通道，结合 `layer.enable` 可在测试平台一次性打开所有层。
- **u9-l3（Debug 层与调试信息）**：本讲提到的 printf 自动入 Debug 层、`DebugMeta` 等调试元数据机制，将在该讲深入。
- **继续阅读源码**：`BoringUtils`（`src/main/scala/chisel3/util/experimental/BoringUtils.scala`）是探针的高层封装（`tap`/`rwTap`/`tapAndRead`），自动在中间模块补探针端口，建议作为「探针实战」的下一站。
