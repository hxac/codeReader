# MMU 与 TLB/PTW

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 Ventus 的「可选 MMU」整体走势：一次访存虚拟地址如何依次经过 `L1TLB → L2TLB → PTW → L2Cache`，最终拿到物理地址，以及在每一级「命中 / 未命中」时分别走哪条路。
- 复述 SV32/SV39 两种页表模式下虚拟地址、物理地址、页表项（PTE）的字段切分，并能手算给定虚拟地址的 VPN/PPN/offset 与各级 PTE 的物理地址。
- 解释 PTW 如何逐级遍历多级页表，并依据 PTE 的标志位（V/R/W）区分「叶子节点」「非叶子（PDE）」「非法（fault）」三种情况。
- 描述 L2TLB 的两个关键优化：扇区化（sector，一次回填连续多条叶子 PTE）与加速表（accel table，缓存中间级 PDE 以跳过上层遍历）。
- 理解 AsidLookup 如何把 ASID 映射到页表基址 PTBR，以及在 ASID 重建时如何触发 TLB 失效。
- 知道 MMU 受 `MMU_ENABLED` 开关控制（默认关闭），以及它在 `GPGPU_top` 中是如何与 L2 缓存复用同一组端口的。

## 2. 前置知识

本讲是 u7 单元（MMU、互联与集成实践）的第一讲，默认你已读过：

- **u2-l2**：`GPGPU_top` 顶层组装与贯穿始终的 **source 字段**贴标/剥标路由机制。本讲里 TLB 与普通访存复用 L2 端口，仍靠 source 的某个位区分彼此。
- **u6-l2 / u6-l3**：L1 DCache 的端口语义（`coreReq/coreRsp` 对 SM、`memReq/memRsp` 对 L2），以及 MSHR 的 primary/secondary miss 合并思想。本讲里的 L2TLB 用了类似「主条目×子条目」的合并手段。
- **u6-l5**：L2 缓存 `Scheduler` 的入口（`in_a/in_d`）与 source_bits。PTW 的页表读请求最终就是从这里挤进 L2、再走到 DDR 的。

先解释本讲会用到的几个操作系统 / 体系结构术语：

- **虚拟地址（VA）/ 物理地址（PA）**：程序看到的是 VA，内存条上的是 PA。MMU 的工作就是把 VA 翻译成 PA。
- **页（page）**：把地址空间切成固定大小的小块（Ventus 默认 4 KiB），以页为单位做地址映射。页内偏移（offset）在翻译时原样保留。
- **页表（page table）**：一张记录「VA 页 → PA 页」映射关系的表，存在内存里。为了节省存储，它通常组织成多级树（SV32 两级、SV39 三级）。
- **页表项 PTE（Page Table Entry）**：页表里的一个条目，包含「下一级页表/物理页的页号 PPN」和一组权限标志位（V/R/W/...）。
- **TLB（Translation Lookaside Buffer）**：页表项的高速缓存，避免每次翻译都去内存里查页表。
- **PTW（Page Table Walker）**：TLB 未命中时，专门「漫步」多级页表、把叶子 PTE 取回来的硬件状态机。
- **ASID（Address Space ID）**：地址空间标识。不同进程的页表不同，ASID 用来区分「这条 TLB 缓存属于哪个地址空间」。
- **PTBR（Page Table Base Register）**：根页表在内存里的物理基址。给定 ASID，要先查到它对应的 PTBR 才能开始遍历。

> 关于默认配置的重要事实：Ventus 默认 **不开 MMU**（`MMU_ENABLED = false`，[ventus/src/top/parameters.scala:15](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L15)）。不开 MMU 时，访存地址就是物理地址，L1 cache 直接用虚拟地址当物理地址访问。本讲讲的所有 TLB/PTW 逻辑，只在 `MMU_ENABLED = true` 时才被例化进硬件。所以读者可以把这一整套当作一个「可选插拔」的能力来读。

## 3. 本讲源码地图

本讲涉及的关键文件全部位于 `ventus/src/mmu/`：

| 文件 | 作用 |
| --- | --- |
| [PTW.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/mmu/PTW.scala) | 既定义了地址/页表格式约定 `SVParam`（含 `SV32`/`SV39`、`PTE`、`FlagBundle`），也实现了页表漫步器 `PTW`。是本讲最核心的文件。 |
| [L1TLB.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/mmu/L1TLB.scala) | 每 SM 私有的全相联一级 TLB `L1TLB`，未命中时向二级 `L2TLB` 求助。 |
| [L2TLB.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/mmu/L2TLB.scala) | 全局共享的二级 TLB `L2TLB`：分 bank、扇区化、带加速表，内部例化 `PTW`；还含两个交叉开关 `L1ToL2TlbXBar`、`L2TlbToL2CacheXBar`。 |
| [AsidLookup.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/mmu/AsidLookup.scala) | ASID → PTBR 的小查找表 `AsidLookup`，并能在 ASID 重建时发出失效信号。 |

此外有两个集成点在顶层 [GPGPU_top.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala) 里：

- [GPGPU_top.scala:224-297](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L224-L297)：`MMU_ENABLED=true` 分支，例化 `L2TLB`、`AsidLookup`、两个交叉开关，并把 PTW 的访存请求仲裁进 L2 入口。
- [GPGPU_top.scala:436-481](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L436-L481)：`SM_wrapper` 内部为 ICache/DCache 各例化一个 `L1TLB`。

整体数据通路可以记成一条单向链：

```
L1 ICache/DCache（给出 VA）
   └─> L1TLB（全相联，每 SM 私有）
         └─> L1ToL2TlbXBar（按 VPN 分 bank）
               └─> L2TLB（分组 + 扇区 + 加速表，全局共享）
                     ├─ AsidLookup 提供 PTBR
                     └─> PTW（多级页表漫步器）
                           └─> L2TlbToL2CacheXBar ── 仲裁进 ──> L2 Scheduler ──> DDR
```

下面逐个最小模块拆开讲。

## 4. 核心概念与源码讲解

### 4.1 SVParam：地址与页表项的格式约定

#### 4.1.1 概念说明

地址翻译的前提是先约定好「虚拟地址怎么切」「物理地址怎么拼」「一个页表项长什么样」。Ventus 用 RISC-V 的 SvMMU 模型，支持两种模式：

- **SV32**：32 位虚拟地址、34 位物理地址、**两级**页表，每级用 10 位索引。这是 Ventus 默认实现（`mmu.SV32`）。
- **SV39**：39 位虚拟地址、56 位物理地址、**三级**页表，每级用 9 位索引。

两种模式只是「地址位宽」和「页表层数」不同，遍历算法完全一样。本讲示例都以默认的 **SV32** 为准。

这些约定被抽成一个 Scala `trait`（`SVParam`），所有 MMU 模块都带着它作为参数 `SV: SVParam`，因此同一份 RTL 既能跑 SV32 也能跑 SV39——这正是后面 `PTW`、`L1TLB`、`L2TLB` 形参列表里那个 `SV` 的来历。

#### 4.1.2 核心流程

SV32 的地址切分如下（位宽自上而下）：

\[
\text{VA}[31{:}12] = \text{VPN} \;(20\,\text{位}) = \underbrace{\text{VA}[31{:}22]}_{\text{VPN}[1]}\;\underbrace{\text{VA}[21{:}12]}_{\text{VPN}[0]},\qquad
\text{VA}[11{:}0] = \text{offset}\;(12\,\text{位})
\]

物理地址：

\[
\text{PA} = \text{PPN}\;(22\,\text{位}) \ll 12 \;|\; \text{offset}
\]

一个 SV32 PTE 共 32 位，结构为 `PPN(22) | reserved(2) | flags(8)`，其中标志位 8 位自高到低是 `D A G U X W R V`（见 `FlagBundle`）。判断 PTE 类型的两条规则非常关键：

- **非叶子（PDE，指向下一级页表）**：`V=1` 且 `R=0` 且 `W=0`（可执行位 X 也可能为 1，但代码里只看 V/R/W）。
- **叶子（指向最终物理页）**：`V=1` 且（`R=1` 或 `W=1`）。
- 其余情况（`V=0`，或非法组合）视为 **page fault**。

给定根页表基址 PTBR，遍历第 `level` 级时，要读的那一项 PTE 的物理地址是：

\[
\text{PTE\_PA} = \text{PPN}_{\text{上一级}} \ll 12 \;|\; \text{VPN}[\text{level}] \ll 2
\]

其中 `<<2` 是因为每个 PTE 占 4 字节。

#### 4.1.3 源码精读

地址约定全部写在 `trait SVParam` 里，`SV32` 和 `SV39` 只是覆写了个别字段：

```scala
trait SVParam{
  def asidLen = 16
  def xLen = 32            // 数据/寄存器位宽
  def vaLen = 32           // 虚拟地址位宽
  def paLen = 34           // 物理地址位宽
  def offsetLen = 12       // 页内偏移（4 KiB 页）
  def ppnLen = paLen - offsetLen   // SV32: 22
  def idxLen = 10          // 每级页表索引位宽（SV32:10, SV39:9）
  def levels = 2           // 页表层数（SV32:2, SV39:3）
  def vpnLen = idxLen * levels     // SV32: 20
  def getVPN(va: UInt): UInt = va(vaLen-1, offsetLen)
  def getVPNIdx(vpn: UInt, level: UInt) = (vpn >> (level * idxLen.U))(idxLen-1, 0)
  def PTE2PPN(pte: UInt): UInt = pte(ppnLen + 10 - 1, 10)   // 取 PTE 里的 PPN 字段
  def PPN2PtePA(ppn: UInt, idx: UInt = 0.U(idxLen.W)): UInt =
    Cat(ppn(ppnLen-1, 0), idx(idxLen-1, 0), 0.U((offsetLen - idxLen).W))  // 算某级 PTE 的物理地址
  ...
}
```

见 [ventus/src/mmu/PTW.scala:19-42](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/mmu/PTW.scala#L19-L42)。`SV32` 直接继承默认值；`SV39` 覆写 `vaLen/paLen/idxLen/levels` 见 [PTW.scala:44-52](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/mmu/PTW.scala#L44-L52)。

`PTE` 与标志位的定义如下，`isPDE`/`isLeaf` 两条判断直接对应 4.1.2 的规则：

```scala
class FlagBundle extends Bundle{
  val D, A, G, U, X, W, R, V = Bool()   // V 在最低位 bit0
}
class PTE extends Bundle with SVParam{
  val reserved1 = UInt((xLen - ppnLen - 10).W)
  val PPN = UInt(ppnLen.W)              // SV32: 22 位
  val reserved2 = UInt(2.W)
  val flag = new FlagBundle()
  def isPDE: Bool = flag.V && !flag.R && !flag.W   // 非叶子：指向下一级
  def isLeaf: Bool = flag.V && (flag.R || flag.W)  // 叶子：最终物理页
}
```

见 [ventus/src/mmu/PTW.scala:61-79](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/mmu/PTW.scala#L61-L79)。注意 `flags(0)` 即 V 位，整个 MMU 都用它当作「这条目是否有效」的依据（TLB 命中、失效判断都看它）。

文件顶部那段 ASCII 图直观给出了 SV39 的布局，可对照阅读：[PTW.scala:7-17](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/mmu/PTW.scala#L7-L17)。

#### 4.1.4 代码实践

**实践目标**：用纸笔把一个具体的 SV32 虚拟地址拆开，算出它的 VPN/offset，并推算根级 PTE 的物理地址，为后面 4.4 节的整条遍历做准备。

**操作步骤**：

1. 取虚拟地址 `VA = 0x0040_1004`。
2. 按 4.1.2 的公式切分：`offset = VA & 0xFFF`，`VPN = VA >> 12`，`VPN[1] = VPN >> 10`，`VPN[0] = VPN & 0x3FF`。
3. 假设根页表基址 `PTBR = 0x0000_2000`，用 `PPN2PtePA` 推算「遍历第 1 级时要读的 PTE 物理地址」。

**需要观察的现象 / 预期结果**：

- `offset = 0x004`，`VPN = 0x401`，`VPN[1] = 1`，`VPN[0] = 1`。
- 根级（level 1）PTE 地址 = `(PTBR>>12)<<12 | VPN[1]<<2 = 0x2000 | (1<<2) = 0x2004`。

这是纯算术推导，无需运行；结果会在 4.4.4 节的完整遍历里再次出现。

#### 4.1.5 小练习与答案

**练习 1**：SV32 一个页表有几项？为什么 PTE 地址里要 `<<2`？
**答**：每级索引 `idxLen=10` 位，故每级页表有 \(2^{10}=1024\) 项。`<<2` 是因为每个 PTE 占 4 字节（32 位），索引乘以 4 才得到字节地址。

**练习 2**：如果一个 PTE 的 `flags = 0b0000_0011`（V=1, R=1, 其余 0），它是叶子还是非叶子？翻译后 PA 怎么取？
**答**：`isLeaf`（V=1 且 R=1）。PA = `PTE2PPN(pte) << 12 | offset`，offset 取自原虚拟地址低 12 位。

---

### 4.2 L1TLB：每个 SM 私有的全相联一级 TLB

#### 4.2.1 概念说明

`L1TLB` 是离 SM 流水线最近的翻译缓存，**每个 SM 内部装两个**——一个给 ICache（指令地址翻译），一个给 DCache（数据地址翻译），对应 `num_cache_in_sm = 2`（[parameters.scala:130](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L130)）。它的设计目标是「小而快」：

- **全相联**（`nSets = 1`，`nWays = 8`）：8 项，任何 VPN 可以放在任意一路，命中率最高，适合容量小的 TLB。
- **只缓存叶子翻译**：存的是 `{asid, vpn, ppn, flags}`，命中时直接 `PA = Cat(ppn, offset)`。
- 未命中就向上层的 L2TLB 求助（`l2_req/l2_rsp` 端口）。

#### 4.2.2 核心流程

`L1TLB` 是一个简单的 5 状态机，每个翻译请求串行走完：

```
s_idle ──in.fire──> s_check
                       │
            ┌──────────┴──────────┐
          hit                  miss
            │                     │
            v                     v
         s_reply <──        s_l2tlb_req ──> s_l2tlb_rsp ──> s_reply
            │
        out.fire
            v
         s_idle
```

- **命中**：把命中项的 `ppn` 与请求的 `offset` 拼成 `paddr`，走 `s_reply` 返回，并更新 LRU。
- **未命中**：把 `{asid, vpn}` 发给 L2TLB（`s_l2tlb_req`→`s_l2tlb_rsp`），收到 `{ppn, flags}` 后**回填**到某一路（优先空闲路，否则按 PseudoLRU 选受害者路），再 `s_reply` 返回。
- `invalidate` 信号有效时，把同 ASID 的所有项清零（V 位置 0）。

#### 4.2.3 源码精读

条目与 IO 定义紧凑，注意命中条件是「ASID 相等 && VPN 相等 && V 位为 1」三者同时成立：

```scala
class L1TlbEntry(SV: SVParam) extends Bundle with L1TlbParam {
  val asid = UInt(SV.asidLen.W)
  val vpn  = UInt(SV.vpnLen.W)
  val ppn  = UInt(SV.ppnLen.W)
  val flags= UInt(8.W)
}
```

见 [L1TLB.scala:17-23](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/mmu/L1TLB.scala#L17-L23)。命中向量与回填路选择：

```scala
val hitVec = VecInit(storage.map{ x =>
  (x.asid === tlb_req.asid) && (x.vpn === SV.getVPN(tlb_req.vaddr)) && x.flags(0)
}).asUInt
val hit = hitVec.orR
...
val refillWay = Mux(avails.asUInt.orR, PriorityEncoder(avails), replace.way)
```

见 [L1TLB.scala:135-141](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/mmu/L1TLB.scala#L135-L141)（`avails` 用「V 位为 0」表示该路空闲）。状态机主体在 [L1TLB.scala:155-199](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/mmu/L1TLB.scala#L155-L199)，其中未命中回填那段把 L2TLB 返回的 ppn/flags 写入 `refillWay`，并拼好返回地址：

```scala
is(s_l2tlb_rsp){
  when(io.l2_rsp.fire){
    storage(refillWay).vpn  := SV.getVPN(tlb_req.vaddr)
    storage(refillWay).asid := tlb_req.asid
    tlb_rsp := Cat(io.l2_rsp.bits.ppn, tlb_req.vaddr(SV.offsetLen-1, 0))  // ppn<<12 | offset
    storage(refillWay).ppn   := io.l2_rsp.bits.ppn
    storage(refillWay).flags := io.l2_rsp.bits.flags
    ...
  }
}
```

见 [L1TLB.scala:178-188](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/mmu/L1TLB.scala#L178-L188)。

> **旁注：「MMU 关闭」时用什么？** 当顶层传入的 `SV = None`（即不开 MMU），`SM_wrapper` 不会例化真正的 `L1TLB`，而是例化一个退化版 `L1TlbAutoReflect`——它把输入的虚拟地址原样当物理地址返回，不做任何翻译，见 [L1TLB.scala:101-119](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/mmu/L1TLB.scala#L101-L119)。这正是「默认 `MMU_ENABLED=false` 时地址不需要翻译」在代码里的体现（例化选择在 [GPGPU_top.scala:437-440](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L437-L440)）。

#### 4.2.4 代码实践

**实践目标**：在源码层面走通「ICache/DCache 与 L1TLB 的四组端口」如何对接，理解 TLB 在 cache 流水线里插入的位置。

**操作步骤**：

1. 打开 [GPGPU_top.scala:436-481](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L436-L481)（`SM_wrapper` 内 `if(MMU_ENABLED)` 块）。
2. 找到这两行，确认 L1TLB[0] 接 ICache、L1TLB[1] 接 DCache：
   ```scala
   l1tlb(0).io.in <> icache.io.TLBReq.get
   icache.io.TLBRsp.get <> l1tlb(0).io.out
   l1tlb(1).io.in <> dcache.io.TLBReq.get
   dcache.io.TLBRsp.get <> l1tlb(1).io.out
   ```
3. 再看上一段（[GPGPU_top.scala:448-453](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L448-L453)），确认两个 L1TLB 的 `l2_req/l2_rsp` 被汇总到 `SM_wrapper.io.l2tlbReq/l2tlbRsp`，向上送给 L2TLB。

**需要观察的现象 / 预期结果**：你能画出 `ICache ↔ L1TLB[0]`、`DCache ↔ L1TLB[1]`、`L1TLB[*] ↔ (向上)L2TLB` 的三组连线，并说出 cache 给 TLB 的是虚拟地址（`TLBReq`），TLB 还给 cache 的是物理地址（`TLBRsp`）。

**预期结果**：明白 MMU 在 cache 之前——cache 拿到 PA 后才去做 tag 比较。

#### 4.2.5 小练习与答案

**练习 1**：L1TLB 命中需要几个周期？未命中（假设 L2TLB 命中）大约需要几个？
**答**：命中走 `s_check→s_reply` 两拍左右；未命中要额外经历 `s_l2tlb_req→s_l2tlb_rsp`（取决于 L2TLB 的握手），拍数明显更多。

**练习 2**：为什么 L1TLB 要存 `asid` 字段而不是只存 `vpn`？
**答**：多个地址空间（进程）可能复用同一个 VPN 但映射到不同物理页。比较时同时匹配 ASID 才不会取错翻译。

---

### 4.3 L2TLB：分组、扇区与加速表的二级 TLB

#### 4.3.1 概念说明

`L2TLB` 是全局共享的二级翻译缓存，容量比 L1TLB 大得多，且做了两个「省访存」的关键优化：

1. **分 bank（`nBanks = 2`）**：把整个 TLB 拆成两个 bank，按 VPN 的某几位路由，两个 SM 的请求可以并行查不同 bank。配套的 `L1ToL2TlbXBar` 负责「多个 L1TLB 请求 → 选 bank」，`L2TlbToL2CacheXBar` 负责「PTW 访存请求 → 选 L2」。
2. **扇区化（sector，`nSectors = 32`）**：一次页表读返回的是一整条 cache 线（32 个字 = 32 个连续 PTE），所以 L2TLB 索性**一条条目缓存 32 个连续叶子 PTE**（`ppns[32]`、`flags[32]`），用 VPN 的低位 `sectorIndex` 选中其中一个。这样一次 miss 能顺便填好相邻 32 个页的翻译，对局部性好的访存极友好。
3. **加速表（`L2TlbAccelStorage`）**：缓存的是**中间级（非叶子，PDE）**的翻译结果。当连续多个虚拟地址的高位 VPN 相同（即落在同一个上层页表节点下），加速表能直接给出「从哪一级开始漫步」，让 PTW 跳过上层、少跑几级页表。

未命中时，L2TLB 例化的 `PTW` 才真正去内存里遍历页表。

#### 4.3.2 核心流程

每个 bank 一个 6 状态机：

```
s_idle ──in.fire──> s_check
                       │
            ┌──────────┴──────────┐
          hit                  miss
            │                     │
         s_reply              s_ptw_req ──> s_ptw_rsp ──> s_reply
```

`s_check` 阶段同时查三样东西，并用 `accelLevel` 选出「最深的命中点」：

- 命中 L2TLB 叶子存储 → `level = 0`（不需要漫步）；
- 命中某个加速表 → `level = 1..levels-1`（从该级开始，还需漫步若干级）；
- 全都没命中 → `level = levels`（从根 PTBR 开始，全程漫步）。

`accelLevel` 决定了 PTW 从哪一级开始、起始物理地址取 PTBR 还是加速表里的 PPN。源码里的注释把这个优先级说得很清楚（见下文引用）。

#### 4.3.3 源码精读

容量参数（来自 `L2TlbParam` trait）：`nSets=16, nWays=4, nSectors=32, nBanks=2`，见 [L2TLB.scala:9-21](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/mmu/L2TLB.scala#L9-L21)。`nSectors` 默认取 `l2cache_BlockWords`（32），与一条 cache 线的字数一致，见 [L2TLB.scala:12](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/mmu/L2TLB.scala#L12)。

每条 L2TLB 条目正是一个「扇区」（一组连续叶子 PTE）：

```scala
class L2TlbEntry(SV: SVParam) extends Bundle with L2TlbParam {
  val vpn = UInt(SV.vpnLen.W)
  val level = UInt(log2Up(SV.levels).W)
  val ppns  = Vec(nSectors, UInt(SV.ppnLen.W))   // 32 个连续叶子的 PPN
  val flags = Vec(nSectors, UInt(8.W))
}
```

见 [L2TLB.scala:23-28](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/mmu/L2TLB.scala#L23-L28)。

`accelLevel` 的优先级编码（命中叶子的下标最低，加速表次之，全 miss 落到 `true.B` 即 level=levels）：

```scala
// for SV39 VA {VPN2, VPN1, VPN0}:
//   accelStorage(1) checks VPN2 and gives out level = 2 when match (2 walks to go)
//   accelStorage(0) checks {VPN2, VPN1} and gives out level = 1 when match (1 walk to go)
//   hit checks whole vpn and gives out level = 0 when match
//   nothing match will gives out level = 3
val accelLevel_pre = PriorityEncoder((hit +: accelStorageArray.map(_.io.accelOut(i).valid)) :+ true.B)
```

见 [L2TLB.scala:305-311](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/mmu/L2TLB.scala#L305-L311)。`accelLevel` 随后被用来挑起始地址（加速表命中取其 PPN，否则取 PTBR）：

```scala
val accelPA = MuxLookup(accelLevel, 0.U)(
  (1 to SV.levels).map(_.U) zip
    (accelOut_delay.map{a => Cat(a(i).bits, 0.U(SV.offsetLen.W))} :+ ptbr_rsp.bits)
)
```

见 [L2TLB.scala:329-332](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/mmu/L2TLB.scala#L329-L332)，并把 `accelLevel-1` 作为 PTW 的起始 `curlevel`（[L2TLB.scala:334-338](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/mmu/L2TLB.scala#L334-L338)）。

主状态机在 [L2TLB.scala:354-418](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/mmu/L2TLB.scala#L354-L418)，其中 `s_ptw_rsp` 拿到 PTW 结果后回填 32 个扇区并返回给 L1（注意回填时只取命中的那个 sector 给 L1，但整组 32 个都存进 L2）：

```scala
is(s_ptw_rsp){
  when(ptw_rsp.fire){
    refillData(i).ppns  := ptw_rsp.bits.ppns
    refillData(i).flags := ptw_rsp.bits.flags
    ...
    tlb_rsp.ppn  := ptw_rsp.bits.ppns(tlb_req.vpn...sectorIndex)   // 选中所请求的扇区
    tlb_rsp.flag := ptw_rsp.bits.flags(...sectorIndex)
    nState := s_reply
  }
}
```

见 [L2TLB.scala:394-405](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/mmu/L2TLB.scala#L394-L405)。

主存储 `L2TlbStorage` 有个巧妙设计：它把「是否有效 + 属于哪个 ASID」单独存成一个小数组 `AsidV`（与放完整 PPN 的大 SRAM 分开），这样失效（invalidate）某个 ASID 时不必读回整条大条目，只清小数组即可——见 [L2TLB.scala:56-107](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/mmu/L2TLB.scala#L56-L107)。加速表 `L2TlbAccelStorage` 在 [L2TLB.scala:134-181](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/mmu/L2TLB.scala#L134-L181)，它用随机替换、按 ASID+高位 VPN 匹配。

#### 4.3.4 代码实践

**实践目标**：理解「一次 L2TLB miss 能填多少条翻译」，把扇区化的收益量化。

**操作步骤**：

1. 读 [L2TLB.scala:9-21](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/mmu/L2TLB.scala#L9-L21) 与 [L2TLB.scala:226-229](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/mmu/L2TLB.scala#L226-L229)，确认 `nSectors` 实际等于 `beatBytes/(xLen/8)`（一条 cache 线的字数）。
2. 想象一段顺序访问 33 个连续页（每页 4 KiB）的访存：第 1 个页触发一次完整 PTW 漫步并回填 32 条；问第 2～32 个页命中哪一级？第 33 个页又如何？

**需要观察的现象 / 预期结果**：

- 一次回填覆盖 32 个连续叶子 PTE。
- 第 2～32 个页都落在同一条 L2TLB 扇区里 → **L2TLB 命中**（level 0），不再访存。
- 第 33 个页属于下一个扇区 → L2TLB 再次 miss，但因为相邻，加速表很可能命中（少走一级），PTW 只需 1 次访存而非完整 2 次。

**预期结果**：能口算出「扇区化把顺序访问的 L2TLB miss 率降为约 1/32」。

#### 4.3.5 小练习与答案

**练习 1**：加速表缓存的是叶子还是非叶子翻译？为什么这样能省事？
**答**：缓存非叶子（PDE）。它记录「这个高位 VPN 前缀对应的下一级页表在哪」，于是高位相同的后续翻译不必从根重新走，直接从中间级开始，减少 PTW 访存次数。

**练习 2**：`accelLevel` 的值域是什么？`accelLevel = SV.levels` 代表什么？
**答**：`0..levels`。`= 0` 表示叶子命中（不用漫步）；`= levels` 表示连加速表也没命中，必须从根 PTBR 开始完整漫步。

---

### 4.4 PTW：按页表逐级遍历的页表漫步器

#### 4.4.1 概念说明

`PTW`（Page Table Walker）是真正「跑腿」的硬件：当 L2TLB 也未命中时，它根据 PTBR，一级一级地到内存里读 PTE，直到读出叶子节点或判定 fault。它是 `L2TLB` 内部例化的一个子模块（`walker`），`Banks` 个并行的漫步器对应 `L2TLB` 的 `nBanks` 个 bank。

PTW 的访存请求不直接连 DDR，而是经 `L2TlbToL2CacheXBar` 挤进 L2 缓存入口——也就是说，**页表本身也被缓存进 L2**，这能大幅减少遍历延迟。

#### 4.4.2 核心流程

每个 bank 一个 5 状态机：

```
s_idle ──ptw_req.fire──> s_memreq ──mem_req.fire──> s_memwait
                                                       │
                          ┌────────────────────────────┼────────────────────┐
                     是 PDE 且未到最底层                       是叶子              非法
                          │                                │                  │
                          v                                v                  v
                     s_memreq（下一级）                  s_rsp            s_fault
```

- 进入 `s_memreq` 时，根据当前级的 PPN 和 VPN 索引算出这一级 PTE 的物理地址（`makePA`），发读请求。
- 收到 PTE（`s_memwait`）后用 `isPDE`/`isLeaf` 判断：
  - **PDE 且 `cur_level > 0`**：还没到底，把 `cur_level` 减一，把读到的 PPN 作为下一级基址，同时向加速表回填（`accel_fill`），回到 `s_memreq` 继续下一级。
  - **叶子**：把 PPN/flags 写入 entry，进入 `s_rsp`。
  - **其它**：page fault，进入 `s_fault`。
- 内存每次返回的是一整条 cache 线（32 个字），PTW 用 `sectorIdx` 选出自己要的那个字当 PTE。

#### 4.4.3 源码精读

PTW 的状态定义与 entry（`PTW.scala:134-161`）。进入 `s_idle→s_memreq` 时记录根信息——注意根级 PPN 来自 `ptw_req.paddr >> offsetLen`（即 PTBR 的页号），并固定 `sectorIdx = 0`：

```scala
when(ptw_req.fire){ // idle -> mem req
  state(i) := s_memreq
  entries(i).cur_level := ptw_req.bits.curlevel
  entries(i).vpn := ptw_req.bits.vpn
  entries(i).ppns(0) := ptw_req.bits.paddr >> SV.offsetLen   // 根 PPN = PTBR>>12
  entries(i).sectorIdx := 0.U // root of a page directory is always sector 0
  entries(i).source := ptw_req.bits.source
  ...
}
```

见 [PTW.scala:170-181](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/mmu/PTW.scala#L170-L181)。

构造某级 PTE 物理地址的两个辅助函数——`makePA` 选中正确的扇区 PPN 再拼上当前级 VPN 索引，`alignedPA` 对齐到内存宽度并算出扇区号：

```scala
def makePA(x: PTWEntry) = SV.PPN2PtePA(x.ppns(x.sectorIdx), SV.getVPNIdx(x.vpn, x.cur_level))
def alignedPA(x: UInt): (UInt, UInt) = {
  val split = log2Up(SV.xLen / 8) + log2Up(nSectors)
  val sectorIdx = x(split - 1, log2Up(SV.xLen / 8))  // 从宽内存响应里定位扇区
  val aligned = Cat( x(x.getWidth - 1, split), 0.U(split.W))
  (aligned, sectorIdx)
}
```

见 [PTW.scala:149-157](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/mmu/PTW.scala#L149-L157)。读请求发出时地址即由它俩算得（[PTW.scala:193-213](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/mmu/PTW.scala#L193-L213)）。

最核心的判断在收到 PTE 那一拍（注意 `accel_fill` 在「PDE 命中、继续下钻」时被拉高，把这一级的 PPN 喂回 L2TLB 的加速表）：

```scala
when(io.mem_rsp(i).fire){
  when(is_memwait(i)){
    when(pte_rsp.isPDE && entries(i).cur_level > 0.U){ // 非叶子：继续下一级
      state(i) := s_memreq
      entries(i).cur_level := entries(i).cur_level - 1.U
      entries(i).ppns := VecInit(mem_rsp_data.map(SV.PTE2PPN))
      io.accel_fill(i).valid := true.B
    }.elsewhen(pte_rsp.isLeaf){                         // 叶子：完成
      entries(i).cur_level := 0.U
      entries(i).ppns  := VecInit(mem_rsp_data.map(SV.PTE2PPN))
      entries(i).flags := VecInit(mem_rsp_data.map(_(7, 0)))
      state(i) := s_rsp
    }.otherwise{                                        // 非法：page fault
      entries(i).fault := true.B
      state(i) := s_fault
    }
  }
}
```

见 [PTW.scala:235-257](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/mmu/PTW.scala#L235-L257)。注意每一步把 `mem_rsp_data`（32 个字）整组存进 `ppns/flags`，这正是 L2TLB 扇区化的数据来源——叶子返回时一并带回相邻 32 个 PTE。

#### 4.4.4 代码实践

**实践目标**：用 4.1.4 的虚拟地址，手算一次 SV32 完整两级遍历，把 PTW 的每一步对应到上面的源码。

**操作步骤**：沿用 4.1.4 设定 `VA = 0x0040_1004`，`PTBR = 0x0000_2000`。假设页表内容如下（数据为示例，仅用于演示翻译过程）：

- 根级（level 1）页表项地址 `0x2004` 处的 PTE = `0x0000_1001`（PPN=0x4，flags=0x1 → V=1,R=0,W=0，是 PDE）。
  > 注：`0x0000_1001` 的 PPN 字段 `PTE2PPN` = `[31:10]` = `0x4`，flags = `[7:0]` = `0x1`。
- level 0 页表项地址 `0x1004` 处的 PTE = `0x0000_2AB7`（PPN=0xA，flags=0x7 → V=1,R=1,W=1，是叶子）。
  > PPN = `[31:10]` = `0xA`，flags = `[7:0]` = `0x7`。

逐步推导：

1. **第 1 级**：`cur_level=1`，根 PPN = `PTBR>>12 = 0x2`。`makePA = PPN2PtePA(0x2, VPN[1]=1) = 0x2<<12 | 1<<2 = 0x2004`。读 `0x2004` → PTE=`0x0000_1001`。
2. 判断：`V=1,R=0,W=0` → `isPDE` 且 `cur_level>0`，下钻：`cur_level:=0`，`ppns:=PTE2PPN=0x4`，回填加速表。
3. **第 0 级**：`cur_level=0`，`makePA = PPN2PtePA(0x4, VPN[0]=1) = 0x4<<12 | 1<<2 = 0x1004`。读 `0x1004` → PTE=`0x0000_2AB7`。
4. 判断：`V=1,R=1` → `isLeaf`，完成。`ppn = PTE2PPN = 0xA`。
5. **最终 PA** = `Cat(ppn=0xA, offset=0x004) = 0xA004`。

**需要观察的现象 / 预期结果**：你能把第 1、3 步的地址计算对应到 `makePA`，把第 2、4 步的分支判断对应到 [PTW.scala:235-257](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/mmu/PTW.scala#L235-L257) 的三个 `when` 分支，最终 PA 拼接对应 L1TLB 里的 `Cat(ppn, offset)`。

**预期结果**：两次 PTW 访存（`0x2004`、`0x1004`），得到叶子 PPN `0xA`，翻译结果 `0xA004`。（以上页表数据为示例代码，非项目真实数据。）

#### 4.4.5 小练习与答案

**练习 1**：SV32 一次完整 miss 的 PTW 要访问几次内存？SV39 呢？
**答**：SV32 最多 2 次（两级），SV39 最多 3 次（三级）。若命中加速表，次数相应减少。

**练习 2**：如果根级 PTE 的 `V=0`，PTW 会怎样？
**答**：既不满足 `isPDE` 也不满足 `isLeaf`，落入 `otherwise` 分支，置 `fault:=true` 进入 `s_fault`，向上报 page fault。

---

### 4.5 AsidLookup：ASID 到页表基址的映射与失效

#### 4.5.1 概念说明

PTW 要开始遍历，必须先知道「根页表在哪」，也就是 PTBR。但 Ventus 里 PTBR 不是写死在一个寄存器里，而是按 ASID 查一张小表得到——这就是 `AsidLookup`。它做两件事：

1. **查表**：给定 ASID，输出对应的 PTBR（页表基址）。
2. **填充 + 失效**：外部（host 或仿真 driver）通过 `fill_in` 安装一条 `{asid, ptbr}`；若该 ASID 已存在，则覆盖并发出 `flush_tlb` 信号，让各级 TLB 把这个 ASID 的旧翻译作废——避免「换了页表却还在用旧缓存」。

这张表很小（默认 8 项，见 [GPGPU_top.scala:226](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L226)），因为活跃的地址空间数量有限。

#### 4.5.2 核心流程

```
fill_in.valid?
   ├─ ASID 已存在：覆盖该条目，并 flush_tlb.valid:=true（带本 ASID）
   └─ ASID 不存在且有空位：写空位（PriorityEncoder 选第一个空槽）
查表（每 bank 同时）：asid 匹配且 valid → 输出 ptbr，否则输出无效
```

#### 4.5.3 源码精读

`AsidLookup` 整个文件只有 43 行，逻辑集中在填充与查询两段。条目定义：

```scala
class AsidLookupEntry(SV: SVParam) extends Bundle{
  val asid = UInt(SV.asidLen.W)
  val ptbr = UInt(SV.xLen.W)
  val valid = Bool()
}
```

见 [AsidLookup.scala:6-10](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/mmu/AsidLookup.scala#L6-L10)。填充时区分「新建」与「覆盖」两种情况，覆盖时发出失效：

```scala
when(io.fill_in.valid){
  when(fill_hitvec.asUInt === 0.U && empty_vec.asUInt =/= 0.U){
    storage(PriorityEncoder(empty_vec)) := io.fill_in.bits        // 新 ASID：写空位
  }.elsewhen(fill_hitvec.asUInt =/= 0.U){                          // 已存在：覆盖
    storage(PriorityEncoder(fill_hitvec)) := io.fill_in.bits
    flush_tlb.valid := true.B
    flush_tlb.bits := io.fill_in.bits.asid
  }
}
```

见 [AsidLookup.scala:27-36](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/mmu/AsidLookup.scala#L27-L36)。每个 bank 的查询是纯组合的：

```scala
(0 until nBanks).foreach{ i =>
  val lookup_hitvec = VecInit(storage.map(e => e.asid === io.lookup_req(i) && e.valid))
  io.lookup_rsp(i).valid := lookup_hitvec.asUInt =/= 0.U
  io.lookup_rsp(i).bits  := Mux(lookup_hitvec.asUInt =/= 0.U, storage(PriorityEncoder(lookup_hitvec)).ptbr, 0.U)
}
```

见 [AsidLookup.scala:37-41](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/mmu/AsidLookup.scala#L37-L41)。

它在顶层的接法（注意 L2TLB 的 `asid_req` 送给 AsidLookup 查询，`ptbr_rsp` 接回结果；`fill_in` 来自顶层 `io.asid_fill`）：

```scala
val asid_lookup = Module(new AsidLookup(SV.get, l2tlb.nBanks, 8))
asid_lookup.io.lookup_req := l2tlb.io.asid_req
l2tlb.io.ptbr_rsp := asid_lookup.io.lookup_rsp
io.asid_fill.foreach{ in => asid_lookup.io.fill_in := in }
```

见 [GPGPU_top.scala:226-231](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L226-L231)。

#### 4.5.4 代码实践

**实践目标**：理解「ASID 复用导致 TLB 失效」这条安全路径，画出信号流。

**操作步骤**：

1. 读 [AsidLookup.scala:27-42](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/mmu/AsidLookup.scala#L27-L42)，确认覆盖时 `flush_tlb` 被拉高。
2. 在顶层 [GPGPU_top.scala:232-237](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L232-L237) 看 `l2tlb.io.invalidate` 是怎么驱动的（当前实现把失效绑定到 `io.host_rsp.valid`，ASID 取自 fill_in 的值）。
3. 追一下：L2TLB 收到 `invalidate` 后会进入 `s_invalid` 状态，并用 4.3.3 提到的 `AsidV` 小数组清掉对应 ASID 的有效位。

**需要观察的现象 / 预期结果**：能复述「`fill_in` 一个已存在的 ASID → `flush_tlb` → L2TLB 失效 →（L1TLB 也会被 `invalidate`）→ 下次翻译重新漫步」这条链。

**预期结果**：明白 AsidLookup 不只是查表，还承担了「地址空间切换时的 TLB 一致性」职责。

> 说明：顶层注释标注 invalidate 的具体触发条件是「待确认」（todo 注释，[GPGPU_top.scala:232](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L232)），当前实现用 `host_rsp.valid` 临时驱动，后续可能调整。

#### 4.5.5 小练习与答案

**练习 1**：AsidLookup 表满了（所有项都 valid）且来了一个新 ASID 的 `fill_in`，会发生什么？
**答**：代码里 `empty_vec` 全 0，第一个 `when` 不成立；`fill_hitvec` 也为 0（新 ASID），第二个分支也不成立。该次 `fill_in` 被丢弃——所以活跃 ASID 数不能超过表容量（默认 8）。

**练习 2**：为什么查表用组合逻辑、而不用寄存器打一拍？
**答**：把 PTBR 查询做成纯组合，L2TLB 在 `s_idle/s_check` 当拍就能拿到 PTBR 启动漫步，少一拍延迟；代价是组合路经稍长，但表很小（8 项）可以接受。

---

### 4.6 整体连接：把 TLB/PTW 串进 L2 缓存

把前面四个模块装回顶层，关键是 **PTW 的访存请求与普通 cache 请求复用同一组 L2 端口**，靠 source 字段区分。这部分逻辑全在 [GPGPU_top.scala:224-297](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L224-L297)：

- `L1ToL2TlbXBar` 把每个 SM 的两个 L1TLB 请求汇总到 L2TLB 的两个 bank，并在请求 id 的最低位贴 `1`（`isTLB` 位），见 [L2TLB.scala:513](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/mmu/L2TLB.scala#L513) 与 [GPGPU_top.scala:264-269](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L264-L269)。
- `tlb_req_arb`（每路 L2 一个 2 输入 Arbiter）把「PTW 经 `L2TlbToL2CacheXBar` 来的请求」与「正常 cluster 互联来的 cache 请求」仲裁进 L2，正常请求的 source 末位补 `0`：见 [GPGPU_top.scala:272-287](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L272-L287)。
- L2 的响应回来时，按 `source(0)` 分流：`=1` 走回 PTW，`=0` 走回正常 cluster 互联，见 [GPGPU_top.scala:289-297](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L289-L297)。

这正好是 u2-l2 / u6-l5 讲过的「source 字段逐层贴标/剥标」在 MMU 场景的复用——只是这里贴的标签语义是「这次访存是 TLB 漫步还是普通数据」。

## 5. 综合实践

**任务**：把本讲全部五个模块串起来，追踪一次「冷启动」地址翻译的端到端过程，并动手让 MMU 真正跑起来。

### 5.1 纸上追踪（必做）

设 `MMU_ENABLED = true`、SV32、`VA = 0x0040_1004`、`PTBR = 0x0000_2000`，页表内容沿用 4.4.4 节示例。请按下列顺序填空并画出时序：

1. **L1TLB**（ICache/DCache 给出 VA）：命中 or miss？→ miss，发 `{asid, vpn=0x401}` 给 L2TLB。
2. **L2TLB**：叶子存储命中 or miss？加速表命中吗？→ 全 miss，`accelLevel = 2`，向 AsidLookup 查 PTBR，向 PTW 发起步信号 `curlevel = 1`。
3. **AsidLookup**：给出 `ptbr = 0x0000_2000`。
4. **PTW**：第 1 级读 `0x2004`（PDE）→ 第 0 级读 `0x1004`（叶子 PPN=`0xA`）→ 回填 L2TLB（含 32 扇区）+ 加速表。
5. **回程**：L2TLB 把 `ppn=0xA` 回给 L1TLB，L1TLB 拼出 `PA = 0xA004` 回给 cache，cache 用 `0xA004` 做 tag 查找。

**预期产出**：一张包含「每级模块、地址/数据、命中情况」的表格，以及一句结论「本次翻译共触发 2 次 L2 访存（PTW 读两級页表），后续访问同扇区的页将命中 L2TLB」。

### 5.2 动手让 MMU 跑起来（选做，待本地验证）

由于 `MMU_ENABLED` 默认是 `false`，要观察 MMU 行为有两条路径：

**路径 A：独立 MMU 测试（推荐，chiseltest）**。仓库在 `ventus/tests/src/MmuTest/` 下有一套独立的 MMU 测试，`MMUSystem` 把 `L1TLB×N + L2TLB + AsidLookup + L1ToL2TlbXBar` 装进一个可测模块，`MMU_test` 的 `"MMU Footprint"` 用例会回放一段访存 footprint 日志、用 `MemBox(SV32)` 建好根页表来喂它：

- 测试模块与例化见 [ventus/tests/src/MmuTest/MMUTest.scala:18-95](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/MmuTest/MMUTest.scala#L18-L95)。
- 生成独立 Verilog 的入口 `MMUGen` 见 [MMUTest.scala:97-100](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/MmuTest/MMUTest.scala#L97-L100)：`new MMUSystem(2, 4, mmu.SV32)`。
- `"MMU Footprint"` 用例见 [MMUTest.scala:160-169](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/MmuTest/MMUTest.scala#L160-L169)，它先 `mem.createRootPageTable()` 建页表再回放 footprint。

操作步骤（待本地验证，因依赖 chiseltest 基础设施与 `make init` 拉取的子模块）：

1. 先 `make init` 拉齐依赖；
2. 按 README/tests 的说明运行 MmuTest（通常 `mill ventus.test` 之类，具体命令以仓库当前说明为准）；
3. 观察日志里 `L1#i MISS/HIT`、`L2#i MISS/HIT/AC` 等行，把它们对到 5.1 的时序表。

**路径 B：把 `MMU_ENABLED` 置 true 重新生成 Verilog**。把 [parameters.scala:15](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L15) 改为 `true`，再 `make verilog`，在生成的 `GPGPU_top.v` 中确认出现了 `L1TLB`/`L2TLB`/`PTW`/`AsidLookup` 子模块实例（说明 MMU 已被例化）。注意：仅生成 RTL 不足以验证功能正确性，真正跑通还需配套的页表建立（软件/driver 侧），完整功能验证以路径 A 为准。

> 无论走哪条路径，运行命令的精确形式请以你本地仓库的 README / Makefile 当前说明为准；本讲不假定已运行过任何命令。

## 6. 本讲小结

- Ventus 的 MMU 是「可选插拔」的，受 `MMU_ENABLED`（默认 `false`）控制；关闭时地址不翻译，L1TLB 退化成 `L1TlbAutoReflect`。
- 地址翻译链是 `L1TLB → L2TLB → PTW → L2Cache → DDR`：L1 全相联且每 SM 私有（ICache/DCache 各一），L2 全局共享、分 bank、扇区化、带加速表。
- 地址与 PTE 格式由 `SVParam`（`SV32`/`SV39`）统一约定；翻译的关键是按 VPN 的各级索引逐级读 PTE，用 V/R/W 区分叶子、非叶子（PDE）、fault。
- `PTW` 是逐级漫步的状态机，每读一级 PTE 都会顺带带回整条 cache 线（32 个连续 PTE），这正是 L2TLB 扇区化的数据来源；PDE 中间结果还能喂给加速表，省掉后续翻译的上层访存。
- `AsidLookup` 把 ASID 映射到 PTBR，并在 ASID 重建时驱动各级 TLB 失效，承担地址空间切换的一致性。
- MMU 的访存请求与普通访存复用 L2 端口，靠 source 字段的最低位（`isTLB`）区分与分流——这是 source 贴标路由在 MMU 场景的复用。

## 7. 下一步学习建议

- **u7-l2（AXI 接口与 host 驱动）**：本讲看到 PTW 的页表读请求最终汇入 L2 的 `out_a/out_d`，下一讲会讲这些请求如何经 `AXI4Adapter` 走到外部 DDR，以及 host 如何经 AXI4-Lite 派发 kernel（其中就包括设置 ASID/PTBR 的入口）。
- **u7-l3（Verilator 仿真框架深入）**：如果你想真正把 MMU 跑起来（综合实践路径 A 的完整版），需要理解 `MemBox(SV32)` 如何建模物理内存与页表，这属于仿真框架的范畴。
- **继续阅读源码**：可带着本讲的链路，回头精读 [L2TLB.scala:492-620](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/mmu/L2TLB.scala#L492-L620) 的两个交叉开关（`L1ToL2TlbXBar`/`L2TlbToL2CacheXBar`），把 source 贴标/剥标细节彻底搞清。
