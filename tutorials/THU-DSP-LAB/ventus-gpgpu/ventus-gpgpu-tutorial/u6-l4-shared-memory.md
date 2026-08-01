# 共享内存 SharedMemory

## 1. 本讲目标

本讲聚焦 Ventus SM 内的片上共享内存（SharedMemory）。它是同一个 SM 上、同一 workgroup 的所有 warp 共享的低延迟暂存区，在物理上是一块多 bank 的 SRAM，而不是 cache。学完本讲你应当能够：

- 说清 SharedMemory 的 bank 化 SRAM 结构、容量与对外接口，并理解它「无 tag、无 miss、不走 L2」的暂存器（scratchpad）本质。
- 解释每个 workgroup 如何靠 `CSR_LDS` 在共享内存里分到一段互不重叠的偏移区间，以及 LSU 如何用 `LDS_BASE` 把地址路由到 sharedmem 还是 dcache。
- 手动推导 `BankConflictArbiter` 检测 bank 冲突、按优先级每周期只服务每 bank 一条 lane、把冲突请求拆成多个周期并按 mask 标注有效 lane 的完整过程，并据此比较有无冲突时的吞吐。

本讲是 u6 缓存单元的一环，但它讲的恰恰是「不是 cache 的那一块」——理解它和 DCache（u6-l2）的区别，是本讲的重点之一。

## 2. 前置知识

在进入源码前，先用通俗语言过一遍几个会反复出现的概念。

**scratchpad（暂存器）vs cache。** Cache 会自动判断「要的数据在不在」，不在就去下层（L2/DDR）取回来并可能替换旧数据，靠 tag 阵列和替换策略实现，对软件透明。SharedMemory 没有这些：它就是一块你可以直接按地址读写的 SRAM，地址算对了就有数据，算错了也「命中」（读到的是别的数据），不会触发任何回填或替换。软件必须自己保证各 workgroup 用的地址不重叠。

**bank 与 bank 冲突。** 把一整块 SRAM 切成若干「银行（bank）」，每个 bank 是一个单端口 SRAM：一个周期内一个 bank 只能服务一次读或写。一条向量访存指令有 32 个 lane，每个 lane 给一个地址，硬件按地址里的某几位（bank index）把每个 lane 的请求派发到对应 bank。如果同一周期内有两个及以上 lane 落进同一个 bank，就是 bank 冲突——这个 bank 一个周期服务不完，只能拆到多个周期。这与你在 u5-l4 学到的「cacheline 合并（coalesce）」是两件事：coalesce 是把同 cacheline 的 lane 合成一个 cache 请求；bank 冲突是 sharedmem 内部一个 bank 一周期只能动一次的限制。

**Decoupled 握手与 instrId。** SharedMemory 的 `coreReq`/`coreRsp` 用 `DecoupledIO`（valid/ready/bits）握手；每条请求带一个 `instrId`，响应也带 `instrId`，LSU 侧的 MSHR 靠它把多拍返回的结果按指令拼回去。这点承接 u5-l4。

**SRAMTemplate 的单端口含义。** Ventus 用 `SRAMTemplate` 封装 SRAM，`singlePort=false` 表示读口、写口独立（但同一个 bank 仍然一周期只能各做一次读、一次写）。本讲里「一个 bank 一周期一个访问」的限制就来自每个 bank 是一块独立的 `SRAMTemplate`。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `ventus/src/L1Cache/ShareMem/ShareMem.scala` | `SharedMemory` 主体：例化 bank 冲突仲裁器、数据 crossbar、`NBanks` 块 bank SRAM 与响应队列，把它们连成流水线。 |
| `ventus/src/L1Cache/ShareMem/BankConflictArbiter.scala` | `BankConflictArbiter`：检测 bank 冲突、每周期为每个 bank 选一条 lane、把剩余 lane 存寄存器留到下周期；还有 `DataCrossbar`、地址/数据选择器等辅助模块。 |
| `ventus/src/L1Cache/ShareMem/ShareMemParameters.scala` | 参数 trait `HasShareMemParameter`：定义 `NSets`/`NWays=1`/`NBanks=NLanes`/`BankIdxBits`/`BankOffsetBits`/`BankWords` 与地址布局约定。 |
| `ventus/src/top/parameters.scala` | 全局参数 `sharedmem_depth=1024`、`sharedmem_BlockWords`、`sharemem_size`、`LDS_BASE=0x70000000`。 |
| `ventus/src/pipeline/LSU.scala` | `AddrCalculate`：算地址、判断是否落进 `[LDS_BASE, +sharemem_size)`，是则发 `to_shared`，否则发 `to_dcache`。 |
| `ventus/src/top/GPGPU_top.scala` | 在 SM 内例化 `SharedMemory` 并与 `pipe.io.shared_req/shared_rsp` 对接。 |
| `ventus/src/L1Cache/L1CacheParameters.scala` | `HasL1CacheParameters`：`WordOffsetBits`/`BlockOffsetBits`/`SetIdxBits` 等地址切段定义（被 SharedMemory 继承）。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：先看 SharedMemory 的整体结构与流水线（4.1），再看地址路由与每 workgroup 的 LDS 偏移占用（4.2），最后深入 BankConflictArbiter（4.3）。

### 4.1 SharedMemory：bank 化 SRAM 与流水线

#### 4.1.1 概念说明

`SharedMemory` 是每个 SM 私有的片上暂存区。与 ICache/DCache 不同，它**不是 cache**：

- **无 tag、无 miss、无替换。** 源码里 tag 模块整段被注释掉了，只有裸 SRAM。地址算对就有数据，不存在「未命中下探 L2」的通路——sharedmem 根本不接 L2。
- **直接映射式平铺寻址。** 参数里 `NWays = 1`，没有路的概念，地址到 SRAM 行是一一对应的。
- **按 bank 切片。** 一整块 SRAM 被切成 `NBanks` 个 bank，每个 bank 是一块独立的 `SRAMTemplate`。一条向量指令的 32 个 lane 由 `BankConflictArbiter` 派发到各 bank。

它对外只有两组 `DecoupledIO`：`coreReq`（LSU 来的请求）与 `coreRsp`（返回 LSU 的结果），完全屏蔽了 warp/lane/bank 的内部细节——对 LSU 而言，sharedmem 就是个「给我一包 32-lane 的请求、我还你一包 32-lane 的结果」的黑盒。

#### 4.1.2 核心流程

`SharedMemory` 内部是一条短流水线，请求从左到右单向流动：

```
coreReq ──► BankConflictArbiter ──► DataCrossbar(写) ──► NBanks 块 SRAM
                                                           │
                       coreRsp ◄── coreRsp_Q ◄── DataCrossbar(读) ◄──┘
```

逐拍看：

1. **st0/st1（仲裁）：** `coreReq` 被 `BankConfArb` 接收，仲裁器算出每个 bank 这一拍服务哪条 lane、是否发生 bank 冲突。
2. **写通路：** 写请求经 `DataCrossbarForWrite` 把每条 lane 的数据路由到它对应的 bank，写入 bank SRAM。
3. **读通路：** 读请求在 bank SRAM 里读出，经 `DataCrossbarForRead` 把每个 bank 的数据广播/路由回各 lane。
4. **响应：** 读结果与「写完成」信号汇入 `coreRsp_Q`（深度 `num_thread`），按 `instrId` + `activeMask` 返回 LSU。

关键点：bank 冲突时，步骤 1 会被「拉长」成多个周期，每个周期只服务每 bank 一条 lane；于是同一条 `coreReq` 会产生多拍 `coreRsp`，每拍带不同的 `activeMask`。这正是 4.3 要详讲的部分。

#### 4.1.3 源码精读

**对外接口只有 coreReq/coreRsp：** [ShareMem.scala:55-59](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/ShareMem/ShareMem.scala#L55-L59) 定义 `SharedMemory` 的 IO，`coreReq` 为 `Flipped(DecoupledIO(ShareMemCoreReq))`、`coreRsp` 为 `DecoupledIO(ShareMemCoreRsp)`。

**请求/响应 Bundle：** [ShareMem.scala:33-53](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/ShareMem/ShareMem.scala#L33-L53) 给出 `ShareMemCoreReq`（`instrId`/`isWrite`/`setIdx`/`perLaneAddr[ NLanes ]`/`data[ NLanes ]`）与 `ShareMemCoreRsp`（`instrId`/`isWrite`/`data`/`activeMask`）。注意每个 lane 各自带一个 `ShareMemPerLaneAddr`（`activeMask`/`blockOffset`/`wordOffset1H`）。

**三个子模块：** [ShareMem.scala:62-65](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/ShareMem/ShareMem.scala#L62-L65) 例化 `BankConflictArbiter` 与两个 `DataCrossbar`（一读一写）。注意第 63 行 `TagAccess` 被注释掉——这就是「无 tag」的直接证据。

**响应队列：** [ShareMem.scala:72-75](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/ShareMem/ShareMem.scala#L72-L75) 中 `DepthCoreRsp_Q = num_thread`，注释点明这个 queue 兼作流水线寄存器（`flow=false`），用来吸收多拍冲突响应。

**NBanks 块 bank SRAM：** [ShareMem.scala:145-173](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/ShareMem/ShareMem.scala#L145-L173) 用 `map` 例化 `NBanks` 块 `SRAMTemplate`：

```scala
val DataAccessesRRsp = (0 until NBanks).map { i =>
  val DataAccess = Module(new SRAMTemplate(
    gen = UInt(8.W),            // 每路 1 字节
    set = NSets * NWays * BankWords,
    way = BytesOfWord,          // 4 个字节「路」，支持按字节写
    singlePort = false, bypassWrite = true))
  DataAccess.io.w.req.bits.data := DataCorssBarForWrite.io.DataOut(i)...
  DataAccess.io.r.req.valid := coreReqisValidRead_comb && BankConfArb.io.dataArrayEn(i)
  ...
  Cat(DataAccess.io.r.resp.data.reverse)   // 4 字节拼回 1 个 word
}
```

每块 bank SRAM：`set = NSets*NWays*BankWords`，`way = BytesOfWord=4`（把一个 32 位 word 拆成 4 个字节「路」，用 `waymask` 实现按字节的部分写），`gen=UInt(8.W)`。读返回的 4 路字节用 `Cat(...reverse)` 拼成 32 位。

**容量核算（默认参数）。** `NSets=sharedmem_depth=1024`、`NWays=1`、`BankWords = BlockWords/NBanks = 32/32 = 1`，所以每个 bank 是 1024 个 word 深，32 个 bank 共 \(1024 \times 32 \times 4\text{B} = 131072\text{B} = 128\text{KiB}\)，正好等于 `sharemem_size`（见 [parameters.scala:93-97](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L93-L97)）。这些数值的来源是 `HasShareMemParameter`：[ShareMemParameters.scala:30-52](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/ShareMem/ShareMemParameters.scala#L30-L52)。

**地址切段与 bank 选择约定。** `HasShareMemParameter` 在注释里画出了地址布局，并定义了 bank 字段：

```
//                                       |   blockOffset  |
//                                 bankOffset        wordOffset
// |32      tag       22|21   setIdx   11|  bankIdx   | 1 0|
```

形式化为：32 位地址切成 `tag | setIdx | blockOffset | wordOffset`；`blockOffset` 内部再切成高位的 `bankOffset` 与低位的 `bankIdx`。`bankIdx` 决定 lane 落进哪个 bank，`setIdx` 决定该 bank SRAM 的行。源码见 [ShareMemParameters.scala:42-52](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/ShareMem/ShareMemParameters.scala#L42-L52)（含 `require(BlockWords>=NBanks)` 与 `BankIdxBits/BankOffsetBits/BankWords`）。`WordOffsetBits/BlockOffsetBits/SetIdxBits` 的定义在继承的 [L1CacheParameters.scala:41-44](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1CacheParameters.scala#L41-L44)。

> 默认下 `NBanks = NLanes = num_thread = 32`，`BlockWords = 32`，故 `BankIdxBits = log2(32) = 5`、`BankOffsetBits = BlockOffsetBits - BankIdxBits = 5 - 5 = 0`、`BankWords = 1`。也就是说**默认配置里一个 block 的每个 word 恰好独占一个 bank**，bank 编号就等于 word 在 block 内的位置；于是「同一个 word 位置（哪怕在不同 setIdx 行）」的多个 lane 会落进同一个 bank 而冲突。

**coreReq.ready 的回压条件：** [ShareMem.scala:194](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/ShareMem/ShareMem.scala#L194)：

```scala
io.coreReq.ready := !RegNext(BankConfArb.io.bankConflict) &&
                    !coreRsp_QAlmstFull && !coreReqisValidWrite_st1
```

三个条件：上一拍没在处理 bank 冲突、响应队列没接近满、没有写未落库。其中第一个就是冲突期间「暂停接收新请求」的开关——冲突没消化完，ready 拉低，LSU 自然把同一请求顶在门口。

#### 4.1.4 代码实践

**实践目标：** 确认 SharedMemory 的物理结构与「无 tag」属性。

**操作步骤：**

1. 打开 `ventus/src/L1Cache/ShareMem/ShareMem.scala`，定位 [L62-L65](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/ShareMem/ShareMem.scala#L62-L65)，确认 tag 模块是注释状态、只例化了 `BankConfArb` 与两个 `DataCrossbar`。
2. 定位 [L145-L173](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/ShareMem/ShareMem.scala#L145-L173)，数出 bank SRAM 的实例数与每块深度（`set`/`way`）。
3. 对照 `ShareMemParameters.scala` 的 [L36-L51](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/ShareMem/ShareMemParameters.scala#L36-L51)，把默认值代入算出总容量。

**需要观察的现象 / 预期结果：** 应得到 `NBanks=32`，每块 bank 为 `1024(set) × 1(BankWords) × 4(way=BytesOfWord)` 字节 = 4 KiB，总计 128 KiB；并且找不到任何 tag 比较或 miss 下探 L2 的逻辑。若你修改 `parameters.scala` 的 `sharedmem_depth`（[L93](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L93)）后重新 `make verilog`，bank SRAM 的深度应随之改变（具体资源量变化待本地验证）。

#### 4.1.5 小练习与答案

**练习 1：** 为什么 SharedMemory 不需要 `L1TagAccess`，而 DCache 必须有？  
**答：** sharedmem 是 scratchpad，地址即物理行，不存在「命中/缺失」概念，也没有替换；DCache 是真 cache，容量小于地址空间，必须靠 tag 判断是否命中、靠替换策略腾位置、miss 时还要下探 L2。

**练习 2：** 把 `num_thread` 从 32 改为 8，`NBanks` 与 `BankWords` 分别变成多少？  
**答：** `NBanks = NLanes = num_thread = 8`，`BlockWords` 仍为 32，故 `BankWords = 32/8 = 4`，`BankIdxBits = 3`、`BankOffsetBits = 2`。此时一个 bank 内每行有 4 个 word，bank 编号由 blockOffset 低 3 位决定。

---

### 4.2 地址路由与每 workgroup 的 LDS 偏移占用

#### 4.2.1 概念说明

sharedmem 的物理容量（默认 128 KiB）远小于整片 GPU 的地址空间。Ventus 用**地址范围**而不是 MMU 来区分一段地址该走 sharedmem 还是 dcache（承接 u2-l1）：凡落在 `[LDS_BASE, LDS_BASE + sharemem_size)` 之内的地址进 sharedmem，其余进 dcache 再下探 L2。

但这 128 KiB 是**整个 SM 上所有驻留 workgroup 共享的**。若两个 workgroup 写同一片地址就会互相踩数据。Ventus 的做法是：CTA 调度器在派发每个 workgroup 时，给它分配一段互不重叠的 LDS（local data share）偏移区间，并把这段区间的基址写进该 workgroup 各 warp 的 `CSR_LDS`。于是软件只需要用「`CSR_LDS` + 自己的偏移」来寻址 sharedmem，硬件/调度器保证不同 workgroup 的区间不重叠。这是软件与调度器的契约，sharedmem 硬件本身不做任何隔离或越界检查（没有 tag，也谈不上保护）。

> 关键结论：sharedmem 的「分区」不在 SharedMemory 模块里实现，而是由 **CTA 调度器的资源分配（u3）+ CSR_LDS（u5-l6）+ 软件寻址约定**三者合力完成。SharedMemory 只负责「按地址读写 SRAM」。

#### 4.2.2 核心流程

一条向量访存指令在 sharedmem 这条路径上的流程：

1. **LSU 算地址：** `AddrCalculate`（u5-l4）为每条 lane 算出完整 32 位地址。
2. **判路由：** 对每个 lane 判断地址是否落在 `[LDS_BASE, LDS_BASE+sharemem_size)`。
3. **全部 shared 才走 shared：** 所有活动 lane 都判定为 shared 时，整条指令走 `to_shared`；否则走 `to_dcache`（即「要么全 shared，要么全 dcache」，不混合）。
4. **拼 ShareMemCoreReq：** 把 setIdx、每 lane 的 blockOffset/wordOffset1H/activeMask、isWrite、data、instrId 打包发给 sharedmem。
5. **sharedmem 处理并返回：** 经 4.1 的流水线，结果按 instrId/activeMask 回到 LSU 的 MSHR 拼装。

#### 4.2.3 源码精读

**LDS_BASE 的定义：** [parameters.scala:136](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L136)：

```scala
val LDS_BASE = 0x70000000  // LDS base address: a hyperparameter used within each SM
```

**LSU 内的路由判定：** [LSU.scala:161-167](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/LSU.scala#L161-L167)：

```scala
is_shared(x) := !reg_save.mask(x) ||
  ( addr(x) >= LDS_BASE.U(32.W) &&
    addr(x) <  (LDS_BASE.U(32.W) + sharedmemory_maxsize) )
all_shared := Mux(reg_save.ctrl.isvec, is_shared.asUInt.andR, is_shared(0))
```

注意两点：① `sharedmemory_maxsize` 由上层 `LSUexe` 传入，取自 `sharemem_size.U`（[LSU.scala:548](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/LSU.scala#L548)），默认即 128 KiB；② `all_shared` 用 **AND 归约**——所有活动 lane 都是 shared 才为真，确保「不混合路由」。`!reg_save.mask(x)` 一项表示「非活动 lane 视为 shared」，这样它不会拖累 `all_shared` 的判定。

**打包 to_shared：** [LSU.scala:213-224](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/LSU.scala#L213-L224) 把 `instrId/setIdx/perLaneAddr(blockOffset, wordOffset1H, activeMask)/data/isWrite` 填入 `io.to_shared.bits`，`io.to_shared.valid := state===s_shared`。

**从 pipe 到 SharedMemory 的对接：** pipe 暴露 `shared_req`/`shared_rsp` 端口（[pipe.scala:40-41](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L40-L41)），真正例化 `SharedMemory` 的是 `GPGPU_top.scala` 的 SM 内部：[GPGPU_top.scala:484-497](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L484-L497)：

```scala
val sharedmem = Module(new SharedMemory()(param))
sharedmem.io.coreReq.bits.setIdx := pipe.io.shared_req.bits.setIdx
sharedmem.io.coreReq.valid       := pipe.io.shared_req.valid
pipe.io.shared_req.ready         := sharedmem.io.coreReq.ready
...
pipe.io.shared_rsp.bits.activeMask := sharedmem.io.coreRsp.bits.activeMask
```

可见 sharedmem 与 dcache 是 SM 内**并列**的两个目标，LSU 的 `AddrCalculate` 在二者间二选一。

#### 4.2.4 代码实践

**实践目标：** 厘清「一个地址到底进 sharedmem 还是 dcache」。

**操作步骤：**

1. 读 [LSU.scala:146-167](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/LSU.scala#L146-L167)，记录 `addr(x)` 的计算与 `is_shared`/`all_shared` 的判定式。
2. 假设 3 个地址：`0x70000000`、`0x7001FFFF`、`0x70020000`，按默认 `sharemem_size=0x20000`（128 KiB）逐个代入 `is_shared`。
3. 在 [GPGPU_top.scala:484-497](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L484-L497) 确认 sharedmem 的 coreRsp.activeMask 回灌到 pipe.io.shared_rsp。

**预期结果：** `0x70000000`（基址）与 `0x7001FFFF`（128 KiB 末字节）判为 shared，`0x70020000`（恰越界 1 字节）判为非 shared → 走 dcache。这印证了路由窗口是「左闭右开」的 `[LDS_BASE, LDS_BASE+sharemem_size)`。

#### 4.2.5 小练习与答案

**练习 1：** 若一条向量 load 的 32 个 lane 中，31 个地址在 shared 区、1 个在 dcache 区，会发生什么？  
**答：** `all_shared` 为假（AND 归约），整条指令走 `to_dcache`，不会进 sharedmem。Ventus 不支持一条指令的 lane 混合路由到两个目标。

**练习 2：** 为什么 sharedmem 不需要像 DCache 那样维护 dirty 位和 WSHR？  
**答：** sharedmem 不接 L2、没有写回下层的需求，数据生灭都在片上 SRAM 里；dirty 追踪与 WSHR（u6-l2）是为「写回 / 写序违规」服务的，sharedmem 无此问题。

---

### 4.3 BankConflictArbiter：bank 冲突检测与多周期拆分

#### 4.3.1 概念说明

`BankConflictArbiter` 是 SharedMemory 最核心、也最绕的模块。它解决一个问题：一条向量指令有 32 个 lane，每个 lane 给一个地址，要把它们派发到 32 个 bank，但**每个 bank 一周期只能服务一个访问**。若多个 lane 落进同一个 bank，就要把它们**拆到多个周期**，每周期只让每 bank 的一条 lane 通过，其余的存到寄存器里下周再来。

源码顶部 [BankConflictArbiter.scala:17-31](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/ShareMem/BankConflictArbiter.scala#L17-L31) 的 Version Note 直白地点出了它的能力边界：

- 假设 `NLanes == NBanks`（默认都为 32）。
- **无法合并**不同 lane 对同一 bank 的字节写请求——会被当成冲突，拆成多周期。
- **无法合并**对完全相同地址的读请求——也会被「可怜地」拆成多周期。

也就是说，本模块的策略是「宁可慢、也要正确」：只要一 bank 多于一个请求，就严格地一周期一 lane。

#### 4.3.2 核心流程

整个仲裁每周期做四件事：**检测 → 选 lane → 预留 → 输出**。

1. **算 bankIdx：** 每个 lane 的 bank 编号 = 其 `blockOffset` 的低 `BankIdxBits` 位。
2. **检测冲突：** 统计每个 bank 有多少活动 lane 请求；若有 bank 的请求数 ≥ 2，则 `bankConflict=1`。
3. **每 bank 选一条 lane：** 对每个 bank，用优先级编码器选**编号最小**的那条活动 lane 本周期服务；其余 lane 标记为「预留」，存入寄存器。
4. **保持与重试：** 一旦进入冲突（`bankConflict_reg=1`），`coreReq.ready` 拉低，新请求进不来；仲裁器在内部对「预留 lane 子集」重复步骤 2~3，直到没有任何 bank 的请求数 ≥ 2，冲突解除。

**用数学描述冲突的代价。** 设活动 lane 对各 bank 的请求数为 \(c_b\)（\(b=0,\dots,NBanks-1\)），总活动 lane 数 \(N=\sum_b c_b\)。

- 是否冲突：\(\exists\, b,\ c_b \ge 2\ \Leftrightarrow\ \max_b c_b \ge 2\)
- 消化这条请求所需周期数（深度）：\[ T = \max_{b} c_b \]
- 有效吞吐（lane/周期）：\[ \text{throughput} = \frac{N}{T} = \frac{\sum_b c_b}{\max_b c_b} \]

无冲突时 \(\max_b c_b = 1\)，吞吐 \(= N\)（最高 32 lane/周期）；最坏情况（全部 lane 撞同一 bank）\(\max_b c_b = N\)，吞吐退化为 1 lane/周期。

**默认配置下的直观结论：** 因为 `bankIdx = blockOffset` 且 `BankWords=1`，两个 lane 撞同一 bank ⟺ 它们的 word 地址在 block 内同位置（即 word 地址 mod 32 相等）。于是：

| 访问模式（每 lane 的 word 步长） | 各 lane 的 bankIdx | 冲突? | 周期数 \(T\) | 吞吐 |
| --- | --- | --- | --- | --- |
| 步长 1（连续 word） | 0,1,2,…,31 全不同 | 否 | 1 | 32 lane/周期 |
| 步长 2 | 0,2,…,30 各 2 lane | 是 | 2 | 16 lane/周期 |
| 步长 32（跨整 block） | 全为 0 | 是 | 32 | 1 lane/周期 |

这就是「GPU shared memory 怕跨步（stride）访存」在 Ventus 上的具体表现。

#### 4.3.3 源码精读

**算 bankIdx（lane → bank）：** [BankConflictArbiter.scala:121-130](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/ShareMem/BankConflictArbiter.scala#L121-L130)：

```scala
(0 until NLanes).foreach{ i =>
  perLaneReq(i).activeMask := io.coreReqArb.perLaneAddr(i).activeMask
  perLaneReq(i).bankIdx := io.coreReqArb.perLaneAddr(i).blockOffset(BankIdxBits-1,0)
  ...
}
```

`bankIdx` 取 `blockOffset` 的低 `BankIdxBits` 位——这正是「bank = word 在 block 内位置」的来源。

**检测冲突：** [BankConflictArbiter.scala:132-160](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/ShareMem/BankConflictArbiter.scala#L132-L160)。核心三步：

```scala
bankIdxMasked(i) := bankIdx1H(i) & Fill(NLanes, laneActiveMask(i))   // 每lane的bank独热,屏蔽非活动lane
perBankReq_Bin(b) := Reverse(Cat(bankIdxMasked.map(_(b))))           // 转置: bank b 上有哪些lane
perBankReqCount(b) := PopCount(perBankReq_Bin(b))                    // bank b 的请求数
perBankReqConflict := perBankReqCount.map(_ > 1.U)                   // 该bank是否冲突
bankConflict := Cat(perBankReqConflict).orR && (enable || bankConflict_reg)
```

注意第 159 行的 `bankConflict_reg`：一旦检测到冲突，下一拍 `bankConflict_reg=1` 会让 `bankConflict` 在「还有预留 lane」时继续保持，直到全部消化。

**每 bank 选一条 lane + 预留剩余 lane：** [BankConflictArbiter.scala:162-178](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/ShareMem/BankConflictArbiter.scala#L162-L178)：

```scala
perBankActiveLaneWhenConflict1H := perBankReq_Bin.map(Cat(_)).map(PriorityEncoderOH(_)) // 每 bank 选编号最小的 lane
...
ReserveLaneWhenConflict1H = (~ActiveLane & laneActiveMask)   // 本周期没被选中的活动 lane => 预留
perLaneConflictReq := Mux(bankConflict_reg, perLaneConflictReq_reg, perLaneReq)         // 冲突中用寄存器子集
when(ReserveLaneWhenConflict1H(i)){
  perLaneConflictReq_reg(i).bankIdx    := perLaneConflictReq(i).bankIdx
  perLaneConflictReq_reg(i).AddrBundle := perLaneConflictReq(i).AddrBundle }
perLaneConflictReq_reg(i).activeMask := ReserveLaneWhenConflict1H(i)   // 预留 lane 的 mask 置 1,其余清 0
```

这段是「多周期拆分」的关键：本周期被服务的 lane 由 `PriorityEncoderOH` 选出（每 bank 一条，编号最小）；未被选中的活动 lane 写进 `perLaneConflictReq_reg`，其 `activeMask` 被设为预留位——下一周期仲裁器就用这个寄存器子集继续，直到没有 bank 的请求数 ≥ 2。

**输出：地址 crossbar、数据 crossbar、bank 使能与有效 lane：** [BankConflictArbiter.scala:180-194](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/ShareMem/BankConflictArbiter.scala#L180-L194)：

```scala
io.addrCrsbarOut  := L2BCrossbar(NBanks, perBankActiveLaneWhenConflict1H, ...)  // 每 bank 取选中 lane 的地址
io.dataCrsbarSel1H:= Mux(isWrite, perBankActiveLaneWhenConflict1H, bankIdxMasked)// 写:每bank选lane数据; 读:广播回各lane
io.dataArrayEn    := perBankActiveLaneWhenConflict1H.map(_.orR)                  // bank 有选中 lane 才使能
io.bankConflict   := bankConflict
io.activeLane     := ActiveLaneWhenConflict1H                                    // 本周期服务的 lane → 响应 activeMask
```

`io.activeLane` 经 `SharedMemory` 里 `ShiftRegister(...,2)` 打两拍后成为响应的 `activeMask`（[ShareMem.scala:132 与 189](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/ShareMem/ShareMem.scala#L189-L189)）。于是同一条请求被拆成多拍响应，每拍只标注该周期真正服务的 lane——LSU 的 MSHR 靠 `instrId` 把它们累加拼回完整结果（承接 u5-l4 的 `currentMask` 归零即收齐）。

**辅助模块 DataCrossbar：** [BankConflictArbiter.scala:60-69](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/ShareMem/BankConflictArbiter.scala#L60-L69)，`DataOut(lane) = Mux1H(Select1H(lane), DataIn)`。写时把每 lane 数据送到对应 bank；读时把每 bank 读出值送回寻址它的 lane。它之所以能「方形」复用（`NBanks==NLanes`），正是 Version Note 里那条假设的体现。

#### 4.3.4 代码实践

**实践目标：** 分析一次发生 bank 冲突的向量 shared 写，描述 `BankConflictArbiter` 如何拆分多周期、如何按 mask 选 lane，并与无冲突情形比较吞吐。（这是本讲的主实践。）

**场景设定：** 32 条 lane 做向量 shared store，`isWrite=1`，`setIdx` 都相同。考察两种步长：

- **场景 A（无冲突）：** lane \(i\) 写 word 地址 \(i\)（步长 1）。各 lane 的 `blockOffset = i mod 32`，即 0,1,…,31，bankIdx 全不同。
- **场景 B（全冲突）：** lane \(i\) 写 word 地址 \(i \times 32\)（步长 32，跨整 block）。各 lane 的 `blockOffset = (i\times32) \bmod 32 = 0\)，bankIdx 全为 0。

**操作步骤（源码阅读型）：**

1. 打开 `BankConflictArbiter.scala`，对照 [L121-L130](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/ShareMem/BankConflictArbiter.scala#L121-L130) 算出场景 A、B 各 lane 的 `bankIdx`。
2. 在 [L132-L160](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/ShareMem/BankConflictArbiter.scala#L132-L160) 画出两个场景的 `perBankReqCount`：
   - 场景 A：每个 bank 的 count=1。
   - 场景 B：bank 0 的 count=32，其余 bank count=0。
3. 在 [L162-L178](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/ShareMem/BankConflictArbiter.scala#L162-L178) 追踪场景 B 的逐周期演化（见下方预期）。
4. 在 `ShareMem.scala` [L194](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/ShareMem/ShareMem.scala#L194) 确认冲突期间 `coreReq.ready` 为 0，新请求进不来。

**预期结果（场景 A）：** 无冲突，`bankConflict=0`。全部 32 lane 一周期写完，产生 1 拍 `coreRsp`，`activeMask` 全 1。吞吐 = 32 lane/周期。

**预期结果（场景 B）逐周期演化：**

- 第 0 拍：coreReq fire，`perBankReqCount(0)=32`，`bankConflict=1`。`PriorityEncoderOH` 在 bank 0 选 lane 0（编号最小）；lane 1~31 进 `perLaneConflictReq_reg`（预留）。
- 第 1 拍：`bankConflict_reg=1`，`coreReq.ready=0`（顶住新请求）；仲裁器对预留子集（lane 1~31）再算，bank 0 选 lane 1，预留 lane 2~31。
- …… 每拍服务 1 条 lane，共需 **32 拍** 才把全部 lane 写完。
- 期间产生 32 拍 `coreRsp`，每拍 `activeMask` 只有 1 位为 1（依次是 lane 0, lane 1, …, lane 31），`instrId` 相同。吞吐 = 32 lane / 32 周期 = **1 lane/周期**。

**对比：** 同样 32 个 shared 写，步长 1 用 1 周期，步长 32 用 32 周期，相差 **32 倍**。这正是 shared memory 上要避免「跨步 = block 大小整数倍」这类访存模式的硬件根因。

> 说明：以上逐拍演化是基于源码逻辑的静态推导；若要在仿真中观测，需用 GVM/Verilator 跑一个会触发步长 32 shared store 的测试用例并 dump 波形——具体波形待本地验证。

#### 4.3.5 小练习与答案

**练习 1：** 场景 B 中，若 lane 0 的 `activeMask=0`（不活动），会怎样改变所需周期数？  
**答：** 不活动 lane 不计入 `perBankReqCount`（被 `bankIdxMasked` 屏蔽）。bank 0 的请求数从 32 变 31，需 31 周期。吞吐 = 31/31 = 1 lane/周期（不变），但总周期数减 1。

**练习 2：** 为什么读操作的数据 crossbar 用 `bankIdxMasked`（允许一 bank 数据广播到多条 lane），却仍然要把相同地址的读拆成多周期？  
**答：** 数据通路物理上能广播，但 `PriorityEncoderOH` 每周期只给每 bank 选一条 lane，且响应 `activeMask` 只标注这一条。其余 lane 虽然数据「碰巧」相同，协议上仍要等下一拍的响应 beat 才被认定有效。Version Note 明确这是「未合并相同地址读」的已知简化。

**练习 3：** 若 `NBanks` 与 `NLanes` 解耦（例如 `NLanes=32, NBanks=16`），本模块需要改哪些地方？  
**答：** Version Note 指出需：① `DataCrossbar` 改为非方形双向；② `bankOffset`/`perBankReqCount` 的位宽按 `NBanks` 重算；③ `ConflictBankReq_w` 的选择逻辑调整。当前版本硬绑定 `NBanks==NLanes`。

## 5. 综合实践

把三个模块串起来，完成一次端到端的 shared 访存跟踪：

**任务：** 编写一段「步长 2」的向量 shared load（每条 lane \(i\) 从 sharedmem 读 word 地址 \(2i\)，假设基址已在 `CSR_LDS` 中、`all_shared` 成立），追踪它从 LSU 到 SharedMemory 再回到 LSU 的完整旅程，并预测其性能。

**要求覆盖：**

1. **路由：** 说明地址为何满足 `is_shared`（落在 `[0x70000000, 0x70020000)`）、`all_shared` 为何为真，引用 [LSU.scala:161-167](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/LSU.scala#L161-L167)。
2. **打包：** 列出 `to_shared` 携带的字段（`instrId/setIdx/perLaneAddr/data/isWrite`），引用 [LSU.scala:213-224](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/LSU.scala#L213-L224)。
3. **bank 分析：** 算出每 lane 的 `bankIdx`（步长 2 → bankIdx 为 0,2,4,…,30，每个 bank 被 2 条 lane 命中），判定 `perBankReqCount` 与 `bankConflict`。
4. **多周期拆分：** 用 4.3.4 的方法预测需 2 周期、产生 2 拍 `coreRsp`，每拍 `activeMask` 各标 16 条 lane。
5. **回写拼装：** 说明 LSU 侧 MSHR 如何按 `instrId` 用两拍的 `activeMask` 累加，直到 `currentMask` 归零收齐（承接 u5-l4）。

**预期产出：** 一张含「路由 → bankIdx 表 → 冲突周期 → 响应 beat 的 activeMask」的跟踪表，以及「吞吐 = 32/2 = 16 lane/周期」的结论。

## 6. 本讲小结

- `SharedMemory` 是每个 SM 私有的 **bank 化 SRAM 暂存器**，不是 cache：无 tag、无 miss、无替换、不接 L2；默认 32 bank × 1024 word = 128 KiB。
- 地址范围 `[LDS_BASE=0x70000000, +sharemem_size)` 内的访问走 sharedmem，其余走 dcache；LSU 的 `AddrCalculate` 用 `all_shared`（AND 归约）保证「不混合路由」。
- 每个 workgroup 靠 `CSR_LDS`（CTA 调度器派发时写入）分到互不重叠的 LDS 偏移，分区由「调度器 + CSR + 软件」三者契约实现，sharedmem 硬件不做隔离。
- `BankConflictArbiter` 按 `bankIdx = blockOffset` 低位把 lane 派到 bank；当某 bank 请求数 ≥ 2 即冲突，每周期用 `PriorityEncoderOH` 只服务每 bank 一条 lane，剩余 lane 存寄存器下周期重试。
- 冲突期间 `coreReq.ready` 拉低、同一条请求产生多拍 `coreRsp`，每拍 `activeMask` 标注本周期服务的 lane，LSU 的 MSHR 靠 `instrId` 拼回完整结果。
- 消化周期数 \(T=\max_b c_b\)，吞吐 \(=N/T\)；步长 1 无冲突（1 周期），步长等于 block 大小则全冲突（32 周期，慢 32 倍）。

## 7. 下一步学习建议

- **横向对比缓存：** 回到 u6-l2（DCache）与本讲对照，体会「写回 + WSHR + dirty」与「裸 SRAM + bank 冲突」两套截然不同的存储机制。
- **纵向打通访存链路：** 结合 u5-l4（LSU）的 MSHR/`currentMask` 机制，重读本讲 4.3 关于「多拍响应按 instrId 拼回」的部分，确认你能在波形上把一条 shared load 的多拍 `coreRsp` 对上 MSHR 表项。
- **参数定制：** 尝试把 `sharedmem_depth` 或 `num_thread` 改小（如 8 线程），重新生成 Verilog，观察 bank 数、`BankIdxBits`、单 bank 深度的变化，巩固 4.1.5 练习 2 的结论。
- **下一讲：** u6-l5 将讲 `L1Cache2L2Arbiter` 与 L2 Scheduler，把 ICache/DCache 的请求汇入 L2——注意 sharedmem 在那里**不出现**，因为它根本不下探 L2。
