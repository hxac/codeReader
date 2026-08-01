# 参数系统与配置机制

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 Ventus 里**两套**「参数机制」各自的位置、用途与区别：全局 `object parameters`（数值常量的真正源头）和源自 rocket-chip 的 **CDE** 配置系统（`Field / View / Parameters / Config`）。
- 读懂 `parameters` 中的关键规模参数（`num_sm / num_warp / num_thread`）、缓存参数（`dcache_* / l2cache_*`），以及由它们推导出的位宽常量。
- 理解 `CTA_SCHE_CONFIG` 子对象如何描述 CTA 调度器的 WG/WF 资源上限。
- 掌握 CDE 的「链式查询」原理，并能追踪一次 `p(RVGParamsKey)` 从「改参数」到「模块拿到值」的完整路径。
- 会用 `ParametersToJson / ParamPrintApp` 把全部参数导出为 JSON，并验证修改参数后 Verilog 位宽的变化。

> 承接：本讲建立在 u2-l2（`GPGPU_top` 顶层与集群互联）之上。上一讲我们看到 `GPGPU_top` 例化多少个 `SM_wrapper`、多少个 `Scheduler`、各端口位宽多大，这些「多少」全部来自本讲讲解的参数系统。

## 2. 前置知识

阅读本讲前，最好先了解以下几个概念（不熟悉也不要紧，下面会结合源码再讲一遍）：

- **Scala `object`**：一个单例对象。`object parameters` 就是一个全局唯一的单例，里面的 `def`/`val`/`var` 在整个项目里都可以用 `parameters.xxx` 访问，相当于 C 里的「一组全局常量」。
- **`def` vs `val` vs `var`**：`def` 每次访问重新求值（像函数）；`val` 初始化一次不可变；`var` 可变、可被重新赋值。`parameters` 里绝大多数是 `def`，但 `num_warp`、`num_thread` 偏偏是 `var`——这是有意为之，后面会解释原因。
- **`log2Ceil(n)`**：Chisel 工具函数，返回表示 `n` 个不同值所需的位数，即 \(\lceil \log_2 n \rceil\)。注意一个易错点：`log2Ceil(4) == 2`，它把 `n` 当作「总数」而非「最大下标」。
- **隐式参数（`implicit`）**：Scala 特性。声明为 `implicit` 的参数可以不在调用处显式写出，编译器会自动从作用域里找类型匹配的值填入。CDE 的 `Parameters` 就是这样在模块间「悄悄」传递的。
- **CDE（Configuration Dependent Environments）**：rocket-chip 社区的一套参数配置框架，核心思想是「把参数组织成一条可拼接的链，查询时从链头往后找」。Ventus 把它精简后放在 `ventus/src/config/config.scala`。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `ventus/src/top/parameters.scala` | 全局 `object parameters`：所有数值常量与位宽推导的源头；内嵌 `CTA_SCHE_CONFIG` 子对象；末尾还有 `ParametersToJson` 与 `ParamPrintApp`。本讲的主角。 |
| `ventus/src/config/config.scala` | CDE 框架的精简实现：定义 `Field`（键）、`View`（只读视图）、`Parameters`（可拼接链）、`Config`（用户可继承的具体配置）。 |
| `ventus/src/L1Cache/L1CacheParameters.scala` | `class MyConfig extends Config(...)`——Ventus 唯一一个把 CDE 键映射到具体参数 case class 的 `Config`，是连接两套参数机制的「桥」。 |
| `ventus/src/L1Cache/RVGParameter.scala` | `RVGParamsKey extends Field[RVGParameters]` 与 `trait HasRVGParameters`：定义键、参数 case class，以及「消费端」如何用 `p(RVGParamsKey)` 取值。 |
| `ventus/src/top/ExtMem_gen.scala` | `object GPGPU_gen`：综合用 Verilog 生成入口，构造 `MyConfig` 并喂给 `GPGPU_top`。 |
| `ventus/src/L2cache/Configs.scala` | `InclusiveCacheKey extends Field[...]`：L2 缓存侧的 CDE 键（注意它用的是**上游版** CDE，见 4.3.1）。 |

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：`parameters`、`CTA_SCHE_CONFIG`、CDE 配置系统（`config.scala`）、`ParametersToJson`。

---

### 4.1 `parameters` 全局对象

#### 4.1.1 概念说明

`object parameters` 是整个 Ventus 硬件描述的「单一事实来源（single source of truth）」：SM 数量、每个 SM 的 warp 数、每 warp 的线程数、各级缓存的组相联度、寄存器堆大小、各种地址位宽……几乎所有「这个硬件有多大」的数字都集中定义在这里。其它源码文件只需 `import top.parameters._` 就能用裸名字（如 `num_warp`）直接引用。

它和 CDE 是两套独立的东西：`parameters` 是朴素的 Scala 全局常量，CDE 是一套带链式查询的框架。两者通过一个「桥」（4.3 节）联系起来。

#### 4.1.2 核心流程

`parameters` 里的值大致分三类，构成一条「定义 → 推导 → 使用」的单向流：

1. **原始规模参数**（人手填的少数几个数字）：`num_sm`、`num_warp`、`num_thread`、各种缓存 `NSets/NWays/BlockWords`。
2. **派生位宽/容量**（由原始参数用 `log2Ceil` 等算出来）：`depth_warp`、`num_vgpr`、`WF_COUNT_WIDTH` 等。改了原始参数，这些自动跟着变。
3. **打包好的缓存参数对象**：`l2cache_params` 等，把一组相关常量塞进一个 case class，供 L2 模块整体取用。

位宽推导的核心公式是：

\[
\texttt{log2Ceil}(n) = \lceil \log_2 n \rceil
\]

例如要表示「最多 8 个 warp 的编号（0..7）」，需要 \(\texttt{log2Ceil}(8)=3\) 位；而要表示「warp 计数（0..8，共 9 个值）」，需要 \(\texttt{log2Ceil}(9)=4\) 位——这就是为什么源码里 `WF_COUNT_WIDTH = log2Ceil(WF_COUNT_MAX + 1)` 要 `+1`。

#### 4.1.3 源码精读

先看最顶部的几个原始规模参数：

[ventus/src/top/parameters.scala:6-9](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L6-L9) 定义了 `object parameters` 并给出三个最关键的规模：`num_sm=2`、`num_warp=8`、`num_thread=32`。注意第 6 行的注释提醒了 `log2Ceil` 的「总数而非下标」语义。还要注意 `num_sm` 是 `def`（不可改写），而 `num_warp`、`num_thread` 是 `var`（可改写）——4.1.4 会解释为什么。

接着看「派生」部分，理解规模如何滚雪球式地决定后续容量与位宽：

[ventus/src/top/parameters.scala:20-30](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L20-L30) 由 `num_warp` 推导出寄存器堆容量（`num_vgpr = 128*num_warp`、`num_sgpr = 256*num_warp`）、bank 深度，以及 warp id 位宽 `depth_warp = log2Ceil(num_warp)`。默认 `num_warp=8` 时，`num_vgpr=1024`、`depth_warp=3`。

再看 L1 数据缓存的参数组（原始 + 派生混在一起）：

[ventus/src/top/parameters.scala:71-90](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L71-L90) 定义了 dcache 的组数 `dcache_NSets=256`、路数 `dcache_NWays=2`、每行字数 `dcache_BlockWords=32`，再由它们推导 `SetIdxBits`、`BlockOffsetBits`、`TagBits`，并给出 MSHR 主/子条目数。`TagBits = xLen - (SetIdxBits + BlockOffsetBits + WordOffsetBits)` 体现了「32 位地址 = tag + set + block offset + word offset」的经典切分。

L2 缓存参数则被进一步**打包成对象**，方便整体传递：

[ventus/src/top/parameters.scala:111-117](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L111-L117) 把 `l2cache_NSets/NWays/BlockWords` 等组装进 `l2cache_cache`、`l2cache_micro`，最终得到 `l2cache_params`（一个 `InclusiveCacheParameters_lite`）。后面 `GPGPU_top` 里出现的 `l2cache_params` 就是它。

> 小贴士：`parameters` 里大量用 `def` 而非 `val`，所以「定义顺序」无所谓——`l2cache_cache`（第 113 行）引用了 `num_l2cache`（第 132 行才出现）也没问题，因为 `def` 只在被调用时才求值。

#### 4.1.4 代码实践

**实践目标**：亲手改一个规模参数，看它如何影响最终 Verilog 的位宽。

**操作步骤**（这是给你做的练习，本讲义不会替你改源码）：

1. 备份后把 `ventus/src/top/parameters.scala:8` 的 `var num_warp = 8` 改成 `var num_warp = 4`。
2. 重新生成仿真用 Verilog：`make verilog`（等价于 `./mill ventus[6.4.0].run`，入口是 `GPGPU_gen`，见 u1-l2）。
3. 在生成的 `GPGPU_top.v`（或 `GPU.v`，因为 `desiredName = "GPU"`）里搜索 warp id 相关位宽，例如 `[2:0]` 与 `[3:0]` 的变化点。

**需要观察的现象**：

- `num_warp` 从 8 改到 4，`depth_warp = log2Ceil(num_warp)` 由 3 变 2，warp 编号位宽应从 3 位降到 2 位。
- `num_vgpr = 128*num_warp` 由 1024 变 512，向量寄存器堆容量减半。
- `WF_COUNT_WIDTH = log2Ceil(num_warp + 1)` 由 `log2Ceil(9)=4` 变成 `log2Ceil(5)=3`。

**预期结果**：多处端口/信号位宽随之收窄。**待本地验证**：具体哪些信号名变化、综合后面积下降多少，需在你本地跑一遍并记录。

> 为什么 `num_warp`/`num_thread` 是 `var`？因为旧的 chiseltest 测试路径会在运行时按测试用例的 metadata 改写它们：[ventus/tests/src/tests.scala:298-300](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/tests/src/tests.scala#L298-L300) 中 `parameters.num_warp = (metas.map(_.wg_size.toInt) :+ testbench.warp).max`。注意 `make test`（chiseltest）已废弃（见 u1-l2/u1-l4），正式仿真走 sim-verilator，这两个值保持默认。

#### 4.1.5 小练习与答案

**练习 1**：默认配置下 `num_sfu` 等于多少？为什么？
**答案**：`num_sfu = (num_thread >> 2).max(1)` = `(32 >> 2).max(1)` = `8`。即每 4 个线程共享 1 个 SFU 单元。

**练习 2**：`sharemem_size` 在默认配置下是多少字节？对应多少 KiB？
**答案**：`sharemem_size = sharedmem_depth * sharedmem_BlockWords * 4` = `1024 * 32 * 4` = `131072` 字节 = 128 KiB（见 [parameters.scala:93-97](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L93-L97)）。

---

### 4.2 `CTA_SCHE_CONFIG` 子对象

#### 4.2.1 概念说明

`CTA_SCHE_CONFIG` 是嵌在 `parameters` 内部的一个子 object，专门给 **CTA 调度器**（u3 单元）描述资源上限。可以把它理解为调度器与硬件签的一份「容量合同」：每个 CU（即 SM）最多挂几个 WG、每个 WG 最多几个 WF、最多吃多少 LDS/SGPR/VGPR……调度器的资源表与分配器都按这份合同工作。

它内部又分四个小块：`GPU`（CU 侧容量）、`WG`（单个 workgroup 的资源上限）、`WG_BUFFER`（缓冲入口数）、`RESOURCE_TABLE`（资源表查询结果数）。

#### 4.2.2 核心流程

`CTA_SCHE_CONFIG` 的值几乎全部来自外层 `parameters`，只是换了个「调度器视角」的名字并补齐上限语义：

```
parameters.num_sm        ─┐
parameters.num_block     ─┼─►  CTA_SCHE_CONFIG.GPU.NUM_CU / NUM_WG_SLOT / NUM_WF_SLOT ...
parameters.num_thread    ─┘
parameters.sharemem_size ─┐
parameters.num_sgpr      ─┼─►  CTA_SCHE_CONFIG.WG.NUM_LDS_MAX / NUM_SGPR_MAX / NUM_VGPR_MAX ...
parameters.num_vgpr      ─┘
```

其中最关键的一个派生量是 **WF tag（wavefront 标签）位宽**：

\[
\texttt{WF\_TAG\_WIDTH\_UINT} = \texttt{log2Ceil}(\text{NUM\_WG\_SLOT}) + \texttt{log2Ceil}(\text{NUM\_WF\_MAX})
\]

即「WG slot 编号」拼上「WG 内的 WF 编号」，用来唯一标识一个被派发出去的 warp。u3-l3 会用到它。

#### 4.2.3 源码精读

先看 `GPU` 块——它描述每个 CU（SM）的容量：

[ventus/src/top/parameters.scala:164-174](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L164-L174) 定义 `NUM_CU = num_sm`、`NUM_WG_SLOT = num_block`（每 CU 同时容纳的 WG 数，默认 8）、`NUM_WF_SLOT = num_warp`、`NUM_THREAD = num_thread`，以及 MMU 相关开关。这些直接决定调度器内部 RAM 的大小。

再看 `WG` 块——它描述单个 workgroup 的资源上限，并推导出 WF tag 位宽：

[ventus/src/top/parameters.scala:175-189](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L175-L189) 给出 `NUM_LDS_MAX = sharemem_size`、`NUM_SGPR_MAX = num_sgpr`、`NUM_VGPR_MAX = num_vgpr`、`NUM_PDS_MAX = 4096*num_thread`，并在末尾算出 `WF_TAG_WIDTH_UINT`。注意第 186 行注释说明了 tag 的拼接方式：`WF tag = cat(wg_slot_id_in_cu, wf_id_in_wg)`。

#### 4.2.4 代码实践

**实践目标**：算清默认配置下一个 CU 的「容量账」。

**操作步骤**：

1. 打开 `parameters.scala`，对照 4.2.3 的两段代码，填一张表：

   | 量 | 默认值 | 来源 |
   |----|--------|------|
   | `GPU.NUM_CU` | 2 | `num_sm` |
   | `GPU.NUM_WG_SLOT` | 8 | `num_block` |
   | `GPU.NUM_WF_SLOT` | 8 | `num_warp` |
   | `WG.NUM_VGPR_MAX` | 1024 | `num_vgpr` |
   | `WG.WF_TAG_WIDTH_UINT` | 6 | `log2Ceil(8)+log2Ceil(8)=3+3` |

2. 思考：若把 `num_warp` 改为 4（沿用 4.1.4 的修改），`WF_TAG_WIDTH_UINT` 变成多少？

**预期结果**：`WF_TAG_WIDTH_UINT = log2Ceil(8) + log2Ceil(4) = 3 + 2 = 5`。**待本地验证**：可用 4.4 节的 JSON 导出快速核对。

#### 4.2.5 小练习与答案

**练习 1**：`NUM_PDS_MAX = 4096*num_thread` 注释里写明这是「per wavefront」而非「per workgroup」的上限。为什么 PDS（私有数据空间）按 wavefront 而非 workgroup 计算？
**答案**：PDS 是每个 warp（wavefront）私有的栈/数据区，线程间不共享，所以按 WF 计量；而 LDS/SGPR/VGPR 在 workgroup 层面按基址+offset 分配给各 WF 共享一个连续区间，故按 WG 计量上限。

**练习 2**：`GPU.NUM_WG_SLOT` 用的是 `num_block`（默认 8），而注释要求 `num_block` 不超过 `num_warp`。为什么？
**答案**：一个 CU 同时驻留的 WG 数不能让总 WF 数超过硬件 warp 槽位 `num_warp`；`num_block <= num_warp` 是防止过订阅的保守上界。

---

### 4.3 CDE 配置系统（`config.scala`）

#### 4.3.1 概念说明

`parameters` 虽好，但它是个全局单例，不适合「不同模块用不同参数」「把参数当对象传来传去」「多层配置相互覆盖」这类需求。rocket-chip 社区为此发明了 **CDE**（Configuration Dependent Environments）。Ventus 在 `ventus/src/config/config.scala` 放了一份精简版，`package config`。

CDE 的三个核心抽象：

- **`Field[T]`**：参数的「键」。每个配置项是一个继承自 `Field` 的对象（通常是 `case object XxxKey extends Field[Yyy]`），类型 `T` 是它对应的值类型。
- **`View`**：只读视图，提供查询接口 `apply(key)`（找不到会报错）和 `lift(key)`（找不到返回 `None`）。
- **`Parameters`**：可拼接的「配置链」。多个 `Parameters` 可以用 `alter` 串成一条链，查询时从链头往后找，第一个命中的即为结果。`Config` 是 `Parameters` 的用户可继承子类。

> 重要区分：Ventus 里其实有**两套** CDE。本讲的 `config.config._`（精简版，供 `top/`、`L1Cache/` 使用）和来自 rocketchip 依赖的 `org.chipsalliance.cde.config._`（上游版，供 `L2cache/` 使用，见 [Configs.scala:15](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L2cache/Configs.scala#L15)）。两者 API 几乎一致，本讲只讲前者；你在 L2 代码里看到 `org.chipsalliance.cde` 不要困惑，那是同一套思想的另一份拷贝。

#### 4.3.2 核心流程

CDE 的查询过程可以用「责任链」来理解。一次 `p(key)` 查询：

```
p(key)
 ├─ 当前 Parameters 节点：用它的 PartialFunction 判断 key 是否定义
 │     ├─ 命中 → 返回对应值
 │     └─ 未命中 → 交给链上的下一个节点（up）
 └─ ……一直找到链尾（TerminalView），还没命中就返回 Field 的默认值或报错
```

`alter` 的拼接方向有个易混点（源码注释反复强调）：`z = x.alter(y)` 得到的 `z`，查询时**先查 `y`，再查 `x`**——即 `y` 的设置「覆盖」`x`。这是一个不可变的纯操作，`x`、`y` 都不被修改。

把这套机制连到 `parameters` 上的「桥」是 `MyConfig`：它把 CDE 的键映射到具体参数 case class，而这些 case class 的默认参数又取自全局 `parameters`：

```
全局 object parameters（数值源头）
        │  （作为 case class 的默认参数）
        ▼
RVGParameters / DCacheParameters / ICacheParameters / ShareMemParameters
        │  （MyConfig 用 PartialFunction 映射）
        ▼
RVGParamsKey / DCacheParamsKey / ICacheParamsKey / ShareMemParamsKey   ← Field 键
        │  （p(RVGParamsKey) 查询）
        ▼
HasRVGParameters trait：模块通过 val RVGParams = p(RVGParamsKey) 取值
```

所以**改 `parameters.num_warp` 会同时影响**：直接引用 `num_warp` 的代码，**以及**经 CDE 路径取 `RVGParams.NWarps` 的代码（因为 `new RVGParameters` 在 elaboration 时重新构造，默认参数读取了最新的 `num_warp`）。

#### 4.3.3 源码精读

先看 `Field`——最简单的抽象，本质是一个带可选默认值的键：

[ventus/src/config/config.scala:11-14](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/config/config.scala#L11-L14) 定义 `Field[T]`，构造时可带默认值（`new Field(default)`）或不带（`new Field()`）。查询到链尾仍未命中时，`TerminalView` 会返回这个默认值（见后文 `TerminalView`）。

再看 `View`——查询的统一入口：

[ventus/src/config/config.scala:18-51](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/config/config.scala#L18-L51) 中，`apply(key)` 调用抽象方法 `find`，找不到就用 `require` 抛错；`lift(key)` 同样调 `find` 但返回 `Option`。模块里到处用的 `p(RVGParamsKey)` 就是这个 `apply`。

接着看 `Parameters` 的链式拼接核心 `alter`：

[ventus/src/config/config.scala:57-84](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/config/config.scala#L57-L84) `alter(rhs)` 返回一个新的 `ChainParameters(rhs, this)`——一个不可变的新链节点。注意 `find` 的实现（第 115-116 行）把 `chain` 的 `up` 参数初始化为 `TerminalView`，即整条链的尽头。

链的内部实现（链头优先、未命中转给 `up`）：

[ventus/src/config/config.scala:186-199](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/config/config.scala#L186-L199) `PartialParameters` 把 `(site, here, up) => PartialFunction` 包成节点：若 PartialFunction `isDefinedAt(pname)` 就返回它的值，否则 `up.find(pname)` 继续往链尾找。`TerminalView`（第 167-169 行）则返回 `Field` 的默认值，终结查询。

最后是用户接口 `Config`：

[ventus/src/config/config.scala:151-163](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/config/config.scala#L151-L163) `Config` 继承 `Parameters`，`toString` 重写为类名（方便调试），并提供 `toInstance`。它的辅助构造器允许直接用 `(site, here, up) => { case ... }` 写法。

现在看 Ventus 里**唯一**的 `Config` 实现——连接两套参数机制的「桥」：

[ventus/src/L1Cache/L1CacheParameters.scala:21-27](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1CacheParameters.scala#L21-L27) `MyConfig` 用一个 `PartialFunction` 把四个键分别映射到 `new DCacheParameters / ICacheParameters / RVGParameters / ShareMemParameters`。这些 case class 的字段默认值都取自 `import top.parameters._`，这就是「桥」。

消费端——定义键与取值的 trait：

[ventus/src/L1Cache/RVGParameter.scala:19-36](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/RVGParameter.scala#L19-L36) 定义 `case object RVGParamsKey extends Field[RVGParameters]`、参数 case class `RVGParameters`（字段如 `NWarps: Int = num_warp`），以及 `trait HasRVGParameters` 用 `val RVGParams = p(RVGParamsKey)` 取值。第 58-59 行的 `RVGBundle / RVGModule` 把 `implicit p: Parameters` 与 `HasRVGParameters` 绑在一起，凡继承它们的模块都自动拥有 `RVGParams`。

elaboration 入口——把 `MyConfig` 喂给 `GPGPU_top`：

[ventus/src/top/ExtMem_gen.scala:22-31](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/ExtMem_gen.scala#L22-L31) `GPGPU_gen` 先 `val L1param = (new MyConfig).toInstance`，再 `new GPGPU_top()(L1param, SV = Some(mmu.SV32))`。而 [GPGPU_top.scala:150-151](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L150-L151) 中 `class GPGPU_top(implicit p: Parameters, ...) extends RVGModule`——于是整棵模块树都通过隐式 `p` 拿到了 `MyConfig`。

把上面串起来，一次完整查询 `p(RVGParamsKey)` 的路径是：
`GPGPU_top` 的隐式 `p` = `L1param` = `MyConfig` → `PartialParameters` 命中 `case RVGParamsKey => new RVGParameters` → 返回 `RVGParameters(NWarps=8, ...)`（8 来自 `parameters.num_warp`）。

#### 4.3.4 代码实践

**实践目标**：用源码阅读的方式走通一次 CDE 查询，不运行、只追踪。

**操作步骤**：

1. 从 [ExtMem_gen.scala:23](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/ExtMem_gen.scala#L23) 的 `(new MyConfig).toInstance` 出发。
2. 翻到 [L1CacheParameters.scala:25](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1CacheParameters.scala#L25) 的 `case RVGParamsKey => new RVGParameters`。
3. 翻到 [RVGParameter.scala:21-32](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/RVGParameter.scala#L21-L32)，确认 `RVGParameters` 各字段默认值（`NSms=num_sm`、`NLanes=num_thread`、`NWarps=num_warp` …）。
4. 翻到 [RVGParameter.scala:36](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/RVGParameter.scala#L36) 的 `val RVGParams = p(RVGParamsKey)`，确认消费端取到的正是上一步构造的对象。

**需要观察的现象**：从「改 `parameters.num_warp`」到「`GPGPU_top` 内的 `NWarps` 变化」之间，数据经过了「全局 object → case class 默认参数 → MyConfig 映射 → 隐式 Parameters → trait 查询」五道关。

**预期结果**：你能用一句话说清——`HasRVGParameters` 里的 `NWarps` 最终等于 `parameters.num_warp`，但取值路径绕了一圈 CDE。**待本地验证**：可在 `RVGParameters` case class 里临时把某字段默认值改成硬编码常量，重新生成 Verilog 观察该字段是否真的走 CDE 路径生效。

#### 4.3.5 小练习与答案

**练习 1**：`z = x.alter(y)`，若 `x` 和 `y` 都定义了同一个键 `K`，`z(K)` 返回谁的值？
**答案**：返回 `y` 的值。`alter` 生成 `ChainParameters(y, x)`，查询时先查链头 `y`，命中即返回，`x` 被覆盖（见 [config.scala:68-69](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/config/config.scala#L68-L69)）。

**练习 2**：如果一个 `Field` 既没给默认值，整条链里也没人定义它，调用 `p(key)` 会怎样？调用 `p.lift(key)` 呢？
**答案**：`p(key)` 走到 `TerminalView` 返回 `None`，`apply` 里 `require(out.isDefined, ...)` 抛出运行时错误（[config.scala:28-30](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/config/config.scala#L28-L30)）；`p.lift(key)` 则安全地返回 `None`。

---

### 4.4 `ParametersToJson` 与 `ParamPrintApp`

#### 4.4.1 概念说明

`parameters` 里有上百个常量，人眼核对很累。`ParametersToJson` 用 Scala 反射自动把 `object parameters` 的所有字段和无参方法提取出来，按名字排序后写成一份 `parameters.json`，方便对照、文档化、以及和软件工具链对账。`ParamPrintApp` 是它的命令行入口。

这同时也回答了一个常见疑问：「我到底改了哪些派生量？」——导出 JSON 前后做 diff，一目了然。

#### 4.4.2 核心流程

```
ParamPrintApp.main
   └─► ParametersToJson.saveToJson("parameters.json")
          ├─ extractAllParameters()：反射遍历 parameters 的所有成员
          │     ├─ 过滤掉系统方法（toString/hashCode/<init> 等）
          │     ├─ 跳过嵌套 object（如 CTA_SCHE_CONFIG）
          │     └─ 对每个 val/var/无参方法求值，转成 JSON
          └─ 用 io.circe 序列化、TreeMap 自动按 key 排序、写文件
```

注意：因为 `extractMemberValue` 遇到 `member.isModule`（嵌套 object）直接返回 `None`（[parameters.scala:260-261](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L260-L261)），所以 `CTA_SCHE_CONFIG` **不会**出现在导出的 JSON 里——导出的是 `parameters` 顶层的扁平字段。

#### 4.4.3 源码精读

导出入口：

[ventus/src/top/parameters.scala:336-339](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L336-L339) `ParamPrintApp extends App`，直接调用 `saveToJson("parameters.json", pretty = true)`。`extends App` 让它能作为程序入口被 `runMain` 执行。

反射提取的核心：

[ventus/src/top/parameters.scala:217-243](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L217-L243) `extractAllParameters` 用 `runtimeMirror` 反射 `parameters`，遍历其成员，过滤系统方法后求值，存入 `TreeMap`（自动按 key 排序）。无法访问的成员被 `catch` 忽略，保证健壮性。

#### 4.4.4 代码实践

**实践目标**：导出当前配置的 `parameters.json`，并验证修改 `num_warp` 后的变化。

**操作步骤**：

1. 在项目根目录运行：`./mill ventus[6.4.0].runMain top.ParamPrintApp`，会在当前目录生成 `parameters.json`。（`make verilog` 走的 `GPGPU_gen` 默认不含 JSON 导出；但仿真入口 `emitVerilog` 会在 `sim-verilator/` 下自动写一份，见 [Mem_SimWrapper.scala:113-114](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/Mem_SimWrapper.scala#L113-L114)。）
2. 复制一份 `parameters.json` 为基线，按 4.1.4 把 `num_warp` 改为 4，重新导出，再 `diff` 两份 JSON。

**需要观察的现象**：diff 会显示 `num_warp`、`num_vgpr`、`num_sgpr`、`depth_warp`、`WF_COUNT_WIDTH`、`WF_TAG_WIDTH_UINT` 等一批键的值同时改变——直观看到「改一个原始参数，派生量联动」。

**预期结果**：得到一份按字母序排列的 JSON，例如包含 `"num_warp" : 4`、`"depth_warp" : 2`。**待本地验证**：`runMain top.ParamPrintApp` 的确切输出路径与 Mill 是否需要先 `make init` 拉依赖，请在你本地环境确认。

#### 4.4.5 小练习与答案

**练习 1**：为什么导出的 JSON 里看不到 `CTA_SCHE_CONFIG` 的内容？
**答案**：`extractMemberValue` 对 `member.isModule`（嵌套 object）返回 `None`，跳过了它（[parameters.scala:260-261](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L260-L261)）。要查 `CTA_SCHE_CONFIG` 的值需直接看源码或 4.2.4 的手算表。

**练习 2**：`saveToJson` 用 `TreeMap` 而不是普通 `Map`，有什么好处？
**答案**：`TreeMap` 按 key 自动排序，让输出的 JSON 字段顺序稳定、可读、便于 diff（[parameters.scala:224](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L224)）。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「参数定制 + 验证」闭环：

1. **选定改动**：把 `parameters.scala` 中 `num_thread` 从 32 改为 8（注意 `tc_dim` 会因此从 `Seq(4,8,4)` 切到 `Seq(2,4,2)`，见 [parameters.scala:119-126](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L119-L126)）。
2. **预测**：在纸上算出改动后这些量的新值：`depth_thread`、`num_sfu`、`num_vgpr`（注意它依赖 `num_warp` 而非 `num_thread`，应**不变**）、`NLanes`（走 CDE 路径，应变为 8）、`sharemem` 相关。
3. **导出核对**：运行 `runMain top.ParamPrintApp`，把 JSON 与你的预测逐项对照。
4. **生成 Verilog 验证位宽**：`make verilog`，在生成的 `GPU.v` 中找到线程掩码（mask）位宽，确认它从 32 位降到 8 位。
5. **追踪 CDE 路径**：按 4.3.4 的步骤，确认 `RVGParameters.NLanes` 的默认值确实取自改动后的 `num_thread`，从而理解「为什么改一处全局变量，CDE 侧也跟着变」。

通过这一闭环，你同时验证了：原始参数定义（4.1）、派生关系、CTA 配置（4.2，虽不进 JSON 但可手算）、CDE 桥接（4.3）、以及 JSON 导出工具（4.4）。

> ⚠️ 这是练习性质的手动改源码。改完验证完请用 `git checkout ventus/src/top/parameters.scala` 还原，避免影响后续学习与仿真。本讲义本身未修改任何源码。

## 6. 本讲小结

- Ventus 有两套参数机制：全局 `object parameters`（数值常量的真正源头）和 CDE 配置系统（`Field/View/Parameters/Config`，源自 rocket-chip）。
- `parameters` 里 `num_sm/num_warp/num_thread` 等少数原始参数，经 `log2Ceil` 等推导出大量位宽与容量（`depth_warp`、`num_vgpr`、`WF_COUNT_WIDTH` 等）；改原始参数，派生量自动联动。
- `CTA_SCHE_CONFIG` 是给 CTA 调度器的「资源合同」，由 `GPU/WG/WG_BUFFER/RESOURCE_TABLE` 四块组成，值全部回溯到外层 `parameters`。
- CDE 是一条可拼接的查询链：`z = x.alter(y)` 中 `y` 覆盖 `x`，查询从链头查到链尾（`TerminalView`）返回默认值或报错。
- `MyConfig` 是连接两套机制的桥：它把 CDE 键映射到参数 case class，而 case class 的默认值取自全局 `parameters`——所以「改 `parameters`」会同时影响直接引用与 CDE 路径。
- `num_warp`/`num_thread` 声明为 `var`，是为了让旧 chiseltest 路径能按 metadata 运行时改写；正式 sim-verilator 仿真中保持默认值。
- `ParametersToJson / ParamPrintApp` 用反射把参数导出为按序排列的 JSON，是核对配置差异的利器（注意它不含嵌套 `CTA_SCHE_CONFIG`）。

## 7. 下一步学习建议

- 本讲建立了「参数如何决定硬件规模」的全局观。下一步进入 **u3 CTA 任务调度器**：你会看到 `CTA_SCHE_CONFIG` 里定义的 `NUM_WG_SLOT / NUM_WF_SLOT / WF_TAG_WIDTH` 是如何被 `wg_buffer`、`resource_table`、`allocator` 真正使用的。
- 想加深对 CDE 的理解，建议读 `ventus/src/L2cache/Configs.scala` 与 `Parameters.scala`，对比上游版 `org.chipsalliance.cde` 与本仓库精简版的异同（u6-l5 会用到）。
- 想了解这些参数如何流向具体模块，可顺着 `HasRVGParameters`（[RVGParameter.scala:34-56](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/RVGParameter.scala#L34-L56)）看 `NSms / NLanes / NWarps` 在 u4（SM 流水线）各模块里的使用。
