# Tag 访问与 MSHR 机制

## 1. 本讲目标

本讲是缓存层次单元（u6）的「机制深挖」篇。u6-l1 讲了 ICache 怎么 miss、u6-l2 讲了 DCache 的写回/写不分配策略，它们都依赖两个底层零件：

- **L1TagAccess**：负责「这块地址在不在 cache 里、如果在哪一路、如果不在踢谁」。
- **L1MSHR**：负责「miss 之后，如何让多个未命中的请求复用同一次内存取数，而不重复打 L2」。

学完本讲，你应当能够：

1. 说清一个物理地址在 Ventus DCache 里被切成 `tag | setIdx | blockOffset | wordOffset` 哪几段，以及对应的 `Bundle` 字段。
2. 读懂 `L1TagAccess` 的 tag SRAM、命中比较器、LRU 替换单元，以及 DCache 专属的 dirty 位与 dirty mask 追踪。
3. 读懂 `L1MSHR` 的「主条目（entry）× 子条目（subentry）」二维结构，说清 **primary miss / secondary miss** 的判定与状态机。
4. 用「两个落同一 cacheline、不同 offset 的 miss」这个例子，解释 secondary miss 如何复用主条目、只发一次 L2 请求。

---

## 2. 前置知识

阅读本讲前，请先具备以下概念（u6-l1 / u6-l2 已建立）：

- **cache 的组相联（set-associative）**：cache 被分成若干「组（set）」，每组有若干「路（way）」。一个地址按 setIdx 映射到某一组，只能放在该组的某一路上。
- **tag 与命中**：组内每一路存一个 tag；访问时把组里所有路的 tag 与请求的 tag 比较，相等且有效即为命中（hit）。
- **MSHR（Miss Status Holding Register）**：缓存 miss 时用来「记住这个未完成请求」的表项。没有它，cache miss 期间流水线只能整个停住（阻塞）；有了它，cache 可以继续接别的请求（非阻塞），等数据回来再回头交付。
- **primary miss / secondary miss**：同一个 cacheline 的第一次 miss 叫 primary miss（要真发请求到下级）；在它还没回来时又来一个落同块的 miss 叫 secondary miss（不用再发请求，挂到已有表项上即可）。
- **cacheline（块）**：cache 与下级内存交换数据的最小单位，Ventus 里一个块 = `BlockWords` 个字（默认 32 字 = 128 字节）。

> **两个 MSHR 文件的澄清（重要）**：仓库里有两套 MSHR 实现，初学者很容易搞混。
> - `ventus/src/L1Cache/ICache/ICacheMSHR.scala` 里的 `class MSHR[T]` 是 **ICache 专用**，带 `miss2mem` 端口、用 `EntryIdx`，已在 u6-l1 讲过。
> - `ventus/src/L1Cache/L1MSHR.scala` 里的 `class MSHR(bABits, tIWidth, ...)` 是 **DCache 用的那一版**，带 `probe` 端口、用 `instrId`。**本讲讲的就是这一版**。
>
> 类名都叫 `MSHR`，靠 `package` 与例化参数区分：DCache 用命名参数 `new MSHR(bABits=..., NMshrEntry, NMshrSubEntry, ...)`，ICache 用类型参数 `new MSHR(UInt(tIBits.W))(p)`。

---

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| [ventus/src/L1Cache/L1Interfaces.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1Interfaces.scala) | DCache 对外（core 侧 / mem 侧）的 Bundle 字典 | `DCacheCoreReq`、`DCacheCoreRsp`、`DCacheMemRsp`、`L1CacheMemReq` 等字段含义 |
| [ventus/src/L1Cache/L1TagAccess.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1TagAccess.scala) | tag 阵列、命中检测、替换、dirty 追踪 | `L1TagAccess`（DCache 版）、`tagChecker`、`ReplacementUnit`、`L1TagAccess_ICache` |
| [ventus/src/L1Cache/L1MSHR.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1MSHR.scala) | DCache 的 MSHR（主/子条目） | `MSHR`、`getEntryStatusReq/Rsp`、primary/secondary miss 状态机 |
| [ventus/src/L1Cache/L1CacheParameters.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1CacheParameters.scala) | 位宽推导 trait | `TagBits`、`SetIdxBits`、`bABits`、`tIBits` 等 |
| [ventus/src/L1Cache/DCache/DCache.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/DCache/DCache.scala) | 把上述零件拼成 DCache 的总装文件 | 用于印证端口如何被真实接线 |

本讲的默认参数（来自 `parameters.scala` 与 `RVGParameter.scala`，`num_thread=32, num_warp=8`）：

```
WordLength = 32        BytesOfWord = 4        WordOffsetBits = 2
dcache_BlockWords = 32  →  BlockOffsetBits = 5
dcache_NSets = 256      →  SetIdxBits = 8
dcache_NWays = 2
TagBits = 32 - (8+5+2) = 17
bABits (块地址) = TagBits + SetIdxBits = 25
NMshrEntry = 4   NMshrSubEntry = 2（DCache 经 DCacheParamsKey 覆盖，默认 trait 里写的是 4）
WIdBits = log2Up(num_warp) = 3      NLanes = num_thread = 32
```

---

## 4. 核心概念与源码讲解

### 4.1 L1Interfaces：core 侧与 mem 侧的 Bundle 字典

#### 4.1.1 概念说明

`L1Interfaces.scala` 不含任何逻辑，它只定义 DCache 与外界交换数据时用的「信封格式」。DCache 有两个邻居：

- **core 侧**（朝向 SM 的 LSU）：用 `DCacheCoreReq` 收请求、`DCacheCoreRsp` 回响应。
- **mem 侧**（朝向 L2）：用 `L1CacheMemReq`/`DCacheMemReq` 发请求、`DCacheMemRsp` 收响应。

把这些字段看懂，是读懂后续 tag 比较与 MSHR 路由的前提——因为「地址怎么切」「source 编号怎么传」都写在这些 Bundle 里。

#### 4.1.2 核心流程：地址切分

一个 32 位物理地址在 DCache 里被切成四段：

```
|<----- tag ----->|<- setIdx ->|<- blockOffset ->|wordOff|
|  bits[31:15] 17 | bits[14:7] 8|   bits[6:2] 5    |[1:0] 2|
```

- `wordOffset`（2 位）：字内字节地址，用来选 4 字节中的哪几个字节（`BytesOfWord=4`）。
- `blockOffset`（5 位）：块内哪一个字（`BlockWords=32`）。由于 `NLanes=32=BlockWords`，blockOffset 同时也就是 bank 编号。
- `setIdx`（8 位）：组索引，决定落到 256 组中的哪一组。
- `tag`（17 位）：组内比较用的高位地址。

**块地址 blockAddr**（MSHR 与 memReq 都用它，而不是完整地址）= `tag | setIdx`，共 `bABits = 25` 位，即地址的 `bits[31:7]`。

#### 4.1.3 源码精读

**core 侧请求**——每条请求是「一条向量访存指令」的所有 lane 地址打包：

[L1Interfaces.scala:L34-L45](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1Interfaces.scala#L34-L45) 定义 `DCacheCoreReq`：`instrId` 标识这条指令；`opcode`（0 读 / 1 写 / 3 flush·invalidate）；`tag`/`setIdx` 是块地址的两段；`perLaneAddr` 是 `Vec(NLanes, DCachePerLaneAddr)`，把 32 个 lane 各自的块内地址与有效位一起带上；`data` 是要写的数据。

[L1Interfaces.scala:L29-L33](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1Interfaces.scala#L29-L33) 定义每个 lane 的地址片 `DCachePerLaneAddr`：`activeMask`（该 lane 是否活跃）、`blockOffset`（落到块内哪个字）、`wordOffset1H`（独热码，选字内字节）。

[L1Interfaces.scala:L47-L52](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1Interfaces.scala#L47-L52) 定义 `DCacheCoreRsp`：回给 LSU 的数据包，同样按 `Vec(NLanes)` 组织，带 `activeMask` 指明哪些 lane 的数据有效。

**mem 侧响应**——L2 回送的是一整块数据，关键在 `d_source`：

[L1Interfaces.scala:L60-L67](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1Interfaces.scala#L60-L67) 定义 `DCacheMemRsp`，其中：

```scala
val d_source = UInt((3+log2Up(NMshrEntry)+log2Up(NSets)).W)  // 共 13 位
val d_data = Vec(BlockWords, UInt(WordLength.W))
```

这 13 位 `d_source` 是 L2 把 DCache 当初请求时填的 `a_source` **原样回送**，DCache 靠它找回「这是哪个 MSHR 主条目、落在哪一组」：

```
d_source 布局（13 位）：
| 3 位(op/tag) | 2 位(MSHR 主条目号) | 8 位(setIdx) |
   bits[12:10]      bits[9:8]            bits[7:0]
```

DCache 总装里正是这么切的（印证）：

- [DCache.scala:L617](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/DCache/DCache.scala#L617)：`missRspIn.bits.instrId := d_source(SetIdxBits+log2Up(NMshrEntry)-1, SetIdxBits)`，即 `d_source[9:8]` 当作 MSHR 主条目号喂给 MSHR。
- [DCache.scala:L621](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/DCache/DCache.scala#L621)：`allocateWrite.bits.setIdx := d_source(SetIdxBits-1, 0)`，即 `d_source[7:0]` 当作 setIdx 去写 tag 阵列。

**mem 侧请求**——`a_source` 与 `d_source` 同宽同布局：

[L1Interfaces.scala:L69-L91](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1Interfaces.scala#L69-L91) 定义 `L1CacheMemReq` 与其子类 `DCacheMemReq`，`a_source` 同样是 `3 + log2Up(MshrEntry) + log2Up(NSets)` 位。MSHR 把主条目编号放进 `a_source`，L2 回包时塞进 `d_source`，于是「请求—响应」配对完成。这个 13 位宽度正是 u2-l2 提到的 `l1cache_sourceBits`（`parameters.scala` 里 `3+log2Up(dcache_MshrEntry)+log2Up(dcache_NSets)`）。

#### 4.1.4 代码实践

**实践目标**：在不跑仿真的前提下，手工切一个地址，确认每段位宽与源码一致。

**操作步骤**：
1. 取一个示例地址，例如 `0x7000_0824`（落在 u2-l1 说的 sharedmem 区，这里只用来练切地址）。
2. 展开成 32 位二进制。
3. 按 `tag[31:15] | setIdx[14:7] | blockOffset[6:2] | wordOffset[1:0]` 切开。

**需要观察的现象 / 预期结果**：

`0x7000_0824 = 0111_0000_0000_0000_0000_1000_0010_0100`

- `wordOffset = bits[1:0] = 00`
- `blockOffset = bits[6:2] = 01001 = 9`（块内第 9 个字）
- `setIdx = bits[14:7] = 0000_0001 = 1`（第 1 组）
- `tag = bits[31:15] = 0_1110_0000_0000_0000 = 0x38000`
- `blockAddr = tag|setIdx = bits[31:7]`

再验证：`BlockOffsetBits(5)+WordOffsetBits(2)=7`，所以块地址正是右移 7 位，与 `L1CacheParameters.scala` 的 `get_blockAddr` 一致（[L1CacheParameters.scala:L66](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1CacheParameters.scala#L66)）。

#### 4.1.5 小练习与答案

**练习 1**：`d_source` 为什么是 13 位？分别来自哪三段？
**答**：`3 + log2Up(NMshrEntry) + log2Up(NSets) = 3 + 2 + 8 = 13`。三段是 3 位 op/tag、2 位 MSHR 主条目号、8 位 setIdx。L2 回包原样回送 `a_source`，DCache 据此找回 MSHR 主条目与目标组。

**练习 2**：为什么 `DCacheCoreReq` 里要带 `Vec(NLanes, DCachePerLaneAddr)`，而不是只带一个基地址？
**答**：因为一条向量访存指令的 32 个 lane 各自有不同地址（可能跨字、跨块），硬件需要每个 lane 的 `blockOffset` 和字节使能才能正确合并（coalesce）与写掩码。这正是 u5-l4 LSU 的职责，DCache 收到的已是合并前的逐 lane 地址。

---

### 4.2 L1TagAccess：tag 存储、命中检测与替换（含 DCache 的 dirty）

#### 4.2.1 概念说明

`L1TagAccess` 是 cache 的「目录」：它存着每一组每一路当前缓存了哪个块（tag）、是否有效（valid）、是否被改过（dirty，仅 DCache）。它回答三个问题：

1. **命中检测**：这个 tag 在不在当前组里？在的话是哪一路？
2. **替换选择**：miss 后要填入新块时，该踢掉这一组的哪一路？
3. **脏位管理**（DCache 专属）：被替换或被无效化的块如果是脏的，要先把数据写回 L2。

它由一个构造参数 `readOnly: Boolean` 控制：`readOnly=true` 时省略所有 dirty 相关结构。注意：实际工程里 ICache 用的是另一个更精简的类 `L1TagAccess_ICache`（见 4.2.3 末），而 `L1TagAccess(readOnly=false)` 专供 DCache，本节重点讲这一路。

#### 4.2.2 核心流程

**命中通路（2 拍流水）**：

```
st0: probeRead 用 setIdx 同时读 tag SRAM（与数据 SRAM）
        │ (SyncReadMem 一拍延迟)
        ▼
st1: tagChecker 把读出的 way 个 tag 与请求 tag 逐路比较（还要 && way_valid）
        │
        ▼
    输出 hit_st1（是否命中）+ waymaskHit_st1（命中的是哪一路，独热）
```

**分配通路（memRsp 回填时）**：

```
allocateWrite(st0) ──► ReplacementUnit 选路（waymaskReplacement_st1）
        │                  ├─ 组未满：选第一个无效路（PriorityEncoder）
        │                  └─ 组已满：选最久未访问的路（LRU，minIdxTree 选最小 access time）
        ▼
st1: 写 tag SRAM + 置 way_valid + 更新 timeAccess（写入当前 accessCount 作为新时间戳）
     若被替换的旧路 dirty=1 → needReplace 拉高，DCache 据此先把脏数据写回 L2
```

**DCache dirty 维护**：

- 写命中：置该路 `way_dirty=1`，并把本次写入的字节掩码 OR 进 `dirtyMaskAccess`（记录块内哪些字节被改过）。
- 替换脏块：输出 `replace_dirty_mask_st1`（旧块的脏字节图），DCache 据此做「部分写回」。
- 无效化/冲刷（invalidate/flush）：扫描所有 `way_dirty`，逐个把脏块写回并清 dirty。

#### 4.2.3 源码精读

**整体 IO 与存储**——[L1TagAccess.scala:L24-L68](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1TagAccess.scala#L24-L68)。注意大量端口被 `if(!readOnly)` 包裹（如 `needReplace`、`hasDirty_st0`、`flushChoosen`、`dirtyMask_st1`），这些是 DCache 专属。

核心存储寄存器：

- [L1TagAccess.scala:L82](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1TagAccess.scala#L82)：`way_valid`，`Vec(set)(Vec(way))` 的有效位阵列。
- [L1TagAccess.scala:L105](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1TagAccess.scala#L105)：`way_dirty`，同结构的脏位阵列（仅 `!readOnly`）。
- [L1TagAccess.scala:L115-L123](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1TagAccess.scala#L115-L123)：`tagBodyAccess`，存 tag 的 SRAM（`set×way`，`bypassWrite=true`、`holdRead=true`）。
- [L1TagAccess.scala:L168-L176](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1TagAccess.scala#L168-L176)：`timeAccess`，存每路「上次访问时间戳」的 SRAM，供 LRU 用（位宽 `Length_Replace_time_SRAM=10`）。
- [L1TagAccess.scala:L209-L217](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1TagAccess.scala#L209-L217)：`dirtyMaskAccess`，存每路「块内字节级脏图」的 SRAM（宽 `dcache_BlockWords*BytesOfWord` 位）。

**读端口仲裁**——DCache 读取 tag SRAM 有三个来源（probe、allocate、hasDirty 扫描），用一个 3 输入 Arbiter 仲裁：

[L1TagAccess.scala:L127-L138](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1TagAccess.scala#L127-L138)：`tagAccessRArb`，端口 1 是 probe（命中查询），端口 0 是 allocate（分配写时的读），端口 2 是 hasDirty（无效化扫描，优先级最低，且仅在前两者都空闲时才发起）。

**命中比较器**——`tagChecker` 是纯组合逻辑：

[L1TagAccess.scala:L380-L410](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1TagAccess.scala#L380-L410)。关键两句：

```scala
tagMatch := Reverse(Cat(io.tag_of_set.zip(io.way_valid).map{
  case (tag,valid) => (tag === io.tag_from_pipe) && valid}))
io.waymask := tagMatch        // 独热，命中了哪一路
io.cache_hit := io.waymask.orR
```

即「tag 相等 **且** 该路有效」才算命中；多路同时命中视为错误（组内不应有重复 tag）。若开启了 MMU，还要再 `& ASIDMatch`（同一虚拟地址空间才匹配）。

**替换单元**——`ReplacementUnit`：

[L1TagAccess.scala:L347-L379](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1TagAccess.scala#L347-L379)。核心是「组未满 vs 组已满」两分：

```scala
io.waymask_st1 := UIntToOH(
  Mux(io.Set_is_full, victimIdx,           // 满：LRU 选最久未用的路
      PriorityEncoder(~io.validOfSet)))    // 未满：选第一个空路
```

其中 `victimIdx` 由 [L1TagAccess.scala:L412-L431](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1TagAccess.scala#L412-L431) 的 `minIdxTree` 用归约树选出「时间戳最小的路」。注意 LRU 用的是「无效路时间戳置 0」的技巧（[L1TagAccess.scala:L359-L361](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1TagAccess.scala#L359-L361)），但这只在「组已满」时被采纳，所以不会误选空路。

**命中输出与 dirty 置位**——[L1TagAccess.scala:L265-L288](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1TagAccess.scala#L265-L288)：把 `tagChecker` 的结果与 `probeReadBuf` 对齐输出 `hit_st1`；并在写命中时 `way_dirty(...) := true.B`，在 flush/replace 时清脏位。

**`needReplace`**——[L1TagAccess.scala:L293-L295](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1TagAccess.scala#L293-L295)：

```scala
io.needReplace.get := way_dirty(allocateWrite_st1.setIdx)(OHToUInt(Replacement.io.waymask_st1))
                       .asBool && RegNext(io.allocateWrite.fire, false.B)
```

即「要被替换的那一路如果是脏的，就告诉 DCache 先写回」——这就是 u6-l2 说的脏块写回三时机之一的「替换」。

**hasDirty 扫描**——[L1TagAccess.scala:L325-L343](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1TagAccess.scala#L325-L343)：把所有组、所有路的 `way_dirty & way_valid` 归约出 `hasDirty_st0`，用 `PriorityEncoder` 选出第一个脏块的 setIdx 与 waymask，供无效化流程逐个清理。注释 [L1TagAccess.scala:L322-L324](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1TagAccess.scala#L322-L324) 解释了为何用「所有组共用一个 priority mux」而非每组一个——成本要低 5~6 倍。

**ICache 的精简版**——`L1TagAccess_ICache`：[L1TagAccess.scala:L432-L509](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1TagAccess.scala#L432-L509)。因为 ICache 只读，它没有 `way_dirty`、`dirtyMaskAccess`、`timeAccess`，替换用更简单的 `ReplacementUnit_ICache`（[L1TagAccess.scala:L510-L522](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1TagAccess.scala#L510-L522)）：未满选第一个空路，满了用一个循环移位的 `victim_1Hidx` 寄存器做伪随机替换，不做 LRU。这正是 u6-l1 说的「ICache 只读故无 dirty/写回、替换即丢弃」的代码出处。

#### 4.2.4 代码实践

**实践目标**：在源码里走通一次「写命中→置 dirty」与「替换脏块→needReplace」。

**操作步骤**：
1. 在 [L1TagAccess.scala:L281-L282](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1TagAccess.scala#L281-L282) 处确认：写命中（`cache_hit && probeIsWrite_st1`）当拍把对应路 `way_dirty` 置 1。
2. 在 [DCache.scala:L315](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/DCache/DCache.scala#L315) 处确认 DCache 把「写命中」连到 `TagAccess.io.probeIsWrite_st1`。
3. 在 [DCache.scala:L573](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/DCache/DCache.scala#L573) 处确认：`tagAllocateWriteReady := !TagAccess.io.needReplace`——即「需要写回脏块时，分配流程要等一等」。

**需要观察的现象 / 预期结果**：一次写命中后该路 `way_dirty=1`；此后若同一组发生 miss 且 LRU 选中了这条脏路，`needReplace` 拉高一拍，DCache 的 allocate 流程被 `!needReplace` 卡住，直到脏块写回 L2 完成。**待本地验证**：可在仿真里给 DCache 加一条 printf 打印 `needReplace` 拉高时刻与对应 setIdx/way。

#### 4.2.5 小练习与答案

**练习 1**：`L1TagAccess` 与 `L1TagAccess_ICache` 的替换策略有何不同？为什么？
**答**：前者（DCache）用 LRU——靠 `timeAccess` SRAM 记录每路上次访问时间戳，组满时选最久未用的路；后者（ICache）不用 LRU，未满选第一个空路，满了用循环移位寄存器做伪随机替换。因为 ICache 只读、替换即丢弃，没有写回代价，替换精度要求低，故用更省面积的简单策略。

**练习 2**：`dirtyMaskAccess` 与 `way_dirty` 各记录什么？为何要分两级？
**答**：`way_dirty` 是「路级」粗粒度脏位（1 位/路），快速判断「这块要不要写回」；`dirtyMaskAccess` 是「字节级」细粒度脏图（`BlockWords*BytesOfWord` 位/路），决定写回时块内哪些字节真正要发。分两级是为了写回时只发脏字节，减少带宽——即 u6-l2 说的「部分写回（PutPartial）」。

---

### 4.3 L1MSHR：主/子条目与 primary/secondary miss 状态机

#### 4.3.1 概念说明

`L1MSHR.scala` 里的 `MSHR` 类是 DCache 的 miss 状态保持寄存器。它要解决一个矛盾：

- cache miss 后必须等下级（L2）把整块数据送回来，这要几十上百拍。
- 这段时间里，如果对 cache 啥也不做（阻塞），整个 SM 的访存就停死了；更糟的是，如果同一条 cacheline 又来了好几个 miss，难道每个都往 L2 发一次请求？

MSHR 的解法是把「未完成的 miss」登记成一张二维表：

- **主条目（primary entry）**：一条「正在等 L2 回送」的 cacheline，存它的 blockAddr。共 `NMshrEntry=4` 条。
- **子条目（subentry）**：挂在一个主条目下、等待同一块回来的多个请求，每个子条目存该请求的 targetInfo（instrId + 各 lane 地址）。每主条目最多 `NMshrSubEntry=2` 条。

于是：
- 第一个 miss 某 cacheline → **primary miss**：新建一个主条目 + 一个子条目，并向 L2 发一次请求。
- 在它回来之前又 miss 同一块 → **secondary miss**：不发包，只在现有主条目下加一个子条目。
- 块回来 → 按子条目逐个把数据交回 core，全部交付完释放主条目。

这就是「非阻塞 + 合并 miss」的关键数据结构。

#### 4.3.2 核心流程

MSHR 内部是一条「probe（st0）→ st1 寄存 → missReq 分配」的小流水：

```
                  blockAddr_Access[entry]   （每主条目存块地址）
   io.probe ──►   比较 probe.blockAddr 与所有主条目 ──► entryMatchProbe（独热，≤1）
   (st0)                   │
                           ▼  latch 进 MSHR_st1 队列
                  entryMatchProbe.orR?  ──yes──► secondary miss（挂到已有主条目）
                           │ no
                           └──────────► primary miss（新建主条目）

   io.missReq ──► 按 st1 结论分配：
                       primary   → entryStatus.next（第一个空主条目），subentry 0
                       secondary → entryMatchProbe 对应主条目，subentryStatus.next（第一个空子条目）
                 并输出 probeOut_st1.a_source = 主条目编号（塞进 a_source 发往 L2）

   io.missRspIn ──► L2 回包（d_source 带回主条目编号）：
                       entryMatchMissRsp = d_source 里的主条目号
                       每拍释放一个子条目 ──► io.missRspOut（把 targetInfo 交回 core）
                       全部子条目清空 ──► io.missRspIn.fire，释放主条目
```

**状态机**（3 位 `mshrStatus`，注释在 [L1MSHR.scala:L160-L168](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1MSHR.scala#L160-L168)）：

| 编码 | 名称 | 含义 |
|------|------|------|
| 000 | PRIMARY_AVAIL | 主条目有空位，可接 primary miss |
| 001 | PRIMARY_FULL | 主条目已满，不能再接 primary miss |
| 010 | SECONDARY_AVAIL | 命中已有主条目且其子条目有空位 |
| 011 | SECONDARY_FULL | 命中已有主条目但其子条目已满 |
| 100 | SECONDARY_FULL_RETURN | 子条目正在逐个归还的过渡态 |

关键控制：`missReq.ready = !(status==1 || status==3)`（[L1MSHR.scala:L295](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1MSHR.scala#L295)）——只有主/子条目都不满时才接收新 miss。

> **两个 status 的分工**：`mshrStatus_st0`（组合逻辑，[L1MSHR.scala:L247-L265](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1MSHR.scala#L247-L265)）给 DCache 在 **st0 拍** 判断「coreReq 还能不能收」（满了就反压 coreReq）；`mshrStatus_st1_r/_w`（寄存器，[L1MSHR.scala:L212-L245](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1MSHR.scala#L212-L245)）给 **st1 拍** 的 `missReq` 握手用。两者服务于不同流水级。

#### 4.3.3 源码精读

**存储与端口**——[L1MSHR.scala:L79-L112](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1MSHR.scala#L79-L112)。MSHR 用寄存器（不是 SRAM）建表：

```scala
val blockAddr_Access   = RegInit(VecInit(Seq.fill(NMshrEntry)(0.U(bABits.W))))      // 主条目块地址
val targetInfo_Accesss = RegInit(VecInit(Seq.fill(NMshrEntry)(
                         VecInit(Seq.fill(NMshrSubEntry)(0.U(tIWidth.W))))))        // 子条目 targetInfo
val subentry_valid     = RegInit(VecInit(Seq.fill(NMshrEntry)(
                         VecInit(Seq.fill(NMshrSubEntry)(false.B)))))               // 子条目有效位
val entry_valid        = Reverse(Cat(subentry_valid.map(Cat(_).orR)))               // 主条目有效位（派生）
```

`entry_valid` 是从 `subentry_valid` 派生的：一个主条目只要还有任何子条目有效，它就有效。文件里的结构示意图 [L1MSHR.scala:L114-L133](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1MSHR.scala#L114-L133) 直观画出了这张二维表。

**primary/secondary 判定**——[L1MSHR.scala:L187-L201](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1MSHR.scala#L187-L201)：

```scala
entryMatchProbe := Reverse(Cat(blockAddr_Access.map(_ === io.probe.bits.blockAddr))) & entry_valid
...
val secondaryMiss_st0 = entryMatchProbe.orR   // 命中已有主条目 → secondary
val primaryMiss_st0   = !secondaryMiss_st0     // 没命中 → primary
```

断言 [L1MSHR.scala:L196](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1MSHR.scala#L196) `PopCount(entryMatchProbe) <= 1` 保证同一块地址在表里最多出现一次（不会有两个主条目存同一块）。

**找空位的辅助模块**——`getEntryStatusReq` / `getEntryStatusRsp`：

- [L1MSHR.scala:L48-L61](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1MSHR.scala#L48-L61)：吃一个 valid 位向量，输出 `full`（全满）、`alm_full`（差一个满）、`next`（第一个 0 的索引，即空位）。MSHR 用它分别找「空主条目」（`entryStatus`）和「某主条目下的空子条目」（`subentryStatus`）。
- [L1MSHR.scala:L63-L72](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1MSHR.scala#L63-L72)：`getEntryStatusRsp` 输出 `next2cancel`（第一个 1 的索引，用于归还时挑要清的子条目）和 `used`（已用计数）。

**分配写入**——[L1MSHR.scala:L297-L301](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1MSHR.scala#L297-L301)：

```scala
val real_SRAMAddrUp   = Mux(secondaryMiss, OHToUInt(entryMatchProbe_st1), entryStatus.io.next)
val real_SRAMAddrDown = Mux(secondaryMiss, MSHR_st1.io.deq.bits.subEntryIdx, 0.U)
when(io.missReq.fire && MSHR_st1.io.deq.ready) {
  targetInfo_Accesss(real_SRAMAddrUp)(real_SRAMAddrDown) := io.missReq.bits.targetInfo
}
```

即 secondary 写到「命中主条目 × 空子条目」，primary 写到「空主条目 × 子条目0」。同时 primary 还要把 `blockAddr_Access` 与 `instrId_Access` 记下（[L1MSHR.scala:L188-L191](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1MSHR.scala#L188-L191)）。

**输出 a_source**——[L1MSHR.scala:L309](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1MSHR.scala#L309)：

```scala
io.probeOut_st1.a_source := Mux(io.missReq.valid, real_SRAMAddrUp, entryMatchProbeid_reg)
```

无论 primary 还是 secondary，`a_source` 都是「这条 miss 归属的主条目编号」。DCache 把它塞进 13 位 `a_source` 发给 L2；L2 回包时这个编号原样出现在 `d_source[9:8]`，MSHR 就能找到对应主条目。

**回包归还**——这是最巧妙的部分。[L1MSHR.scala:L312-L339](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1MSHR.scala#L312-L339)：

```scala
entryMatchMissRsp := io.missRspIn.bits.instrId      // d_source 里带回的主条目号
subentryStatusForRsp.io.valid_list := Reverse(Cat(subentry_valid(entryMatchMissRsp)))
...
io.missRspIn.ready := !((subentryStatusForRsp.io.used >= 1.U) || ... )  // 见下
missRspOut_st1.io.enq.valid := io.missRspIn.valid && !(subentryStatusForRsp.io.used === 0.U)
```

意思是：L2 回包期间（`missRspIn.valid` 持续有效，因为 `memRsp_Q` 把整块数据 hold 住），**每拍** `missRspOut` 输出一个子条目的 `targetInfo` 给 core（DCache 据此从已回填的 cacheline 里取出对应字节回 LSU），并清掉该子条目；只要还有子条目（`used >= 1`），`missRspIn.ready` 就保持假，**不消费** memRsp_Q；直到所有子条目都归还（`used == 0`），`missRspIn.ready` 才拉真，`missRspIn.fire` 一拍消费掉 memRsp_Q 的块并释放主条目（`entry_valid` 因 `subentry_valid` 全空而自动清零）。

> **一句话总结归还机制**：一次 L2 块回送，被「按子条目数」展成多拍 `missRspOut`，让所有挂在这个主条目上的 secondary miss 都拿到数据，然后才释放主条目。所以 secondary miss 「免费搭车」，不重复发 L2 请求。

**子条目维护的完整 when 链**——[L1MSHR.scala:L345-L358](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1MSHR.scala#L345-L358) 把「分配时置 1」与「归还时清 0」写在同一个 when/elsewhen 链里，注释提醒「when 与 elsewhen 的顺序很重要」。

#### 4.3.4 代码实践

**实践目标**：手工推演「两个落同一 cacheline、不同 offset 的 miss」如何复用主条目。这是本讲的主线实践，**源码阅读型**（无需运行仿真）。

**场景设定**（默认参数 `NMshrEntry=4, NMshrSubEntry=2`）：

- 请求 A：blockAddr = `B`（某 cacheline 块地址），instrId = `iA`，要读 blockOffset=3 的字。
- 请求 B：blockAddr = `B`（**同一个 cacheline**），instrId = `iB`，要读 blockOffset=9 的字。
- 假设 A 先到，B 在 A 的数据回来之前到。

**操作步骤 / 逐拍推演**：

1. **A 到达，MSHR 表为空**：`probe(B)` 比较 `blockAddr_Access` 全不相等 → `entryMatchProbe=0` → `primaryMiss`。状态机走 `PRIMARY_AVAIL(000)`。
   - `missReq.fire`：在主条目 0 写 `blockAddr_Access[0]=B`、`instrId_Access[0]=iA`；`targetInfo_Accesss[0][0]=A 的 targetInfo`；`subentry_valid[0][0]=1`。
   - `a_source = 0`（主条目号）。DCache 发 memReq 到 L2，`a_source` 低 2 位 = `00`。
2. **B 到达，A 尚未回包**：`probe(B)` 比较 → 与主条目 0 的 `B` 相等 → `entryMatchProbe = 0001`（独热指向主条目 0）→ `secondaryMiss`。状态机走 `SECONDARY_AVAIL(010)`（子条目 1 还空）。
   - `missReq.fire`：`real_SRAMAddrUp = OHToUInt(0001) = 0`，`real_SRAMAddrDown = subentryStatus.next = 1`。写 `targetInfo_Accesss[0][1]=B 的 targetInfo`、`subentry_valid[0][1]=1`。
   - **不发新的 memReq**（B 复用主条目 0，a_source 仍是 0，但 L2 那边没有第二次请求）。
3. **L2 回包**：`d_source[9:8]=00` → `entryMatchMissRsp=0`。此时 `subentry_valid[0]=[1,1]`，`used=2`。
   - 第 1 拍：`missRspOut` 输出子条目 0（A 的 targetInfo，含 instrId=iA 与 perLaneAddr），DCache 据此把 blockOffset=3 的字回给 A 对应的 LSU 请求；清 `subentry_valid[0][0]=0`。`used=1`，`missRspIn.ready` 仍为假。
   - 第 2 拍：`missRspOut` 输出子条目 1（B 的 targetInfo），DCache 把 blockOffset=9 的字回给 B；清 `subentry_valid[0][1]=0`。`used=0`。
   - 第 3 拍：`missRspIn.ready` 拉真（`used==0`），`missRspIn.fire` 消费 memRsp_Q；主条目 0 因 `subentry_valid[0]` 全空而 `entry_valid[0]=0`，主条目被释放。

**需要观察的现象 / 预期结果**：

- 全程只向 L2 发了 **1 次** memReq（A 的 primary miss）；B 的 secondary miss 没有产生额外 L2 请求。
- 块回来后被展成 **2 拍** `missRspOut`（等于该主条目下的子条目数），A、B 各拿一拍数据。
- 主条目 0 在最后一拍才被释放。

**MSHR 状态转移图（本场景）**：

```
表空
  │ A(primary) 分配主条目0
  ▼
PRIMARY_AVAIL(000) [主条目0: sub={A}]
  │ B(secondary) 匹配主条目0，挂子条目1
  ▼
SECONDARY_AVAIL(010) [主条目0: sub={A,B}]
  │ L2 回包，逐拍归还子条目
  ▼
SECONDARY_FULL_RETURN(100) → 归还完毕
  │ used==0, missRspIn.fire
  ▼
表空（主条目0 释放）
```

> 若在 B 之后又来了第三个同块 miss C，则 `subentry_valid[0]` 会变成 `[1,1]`（已满），状态转 `SECONDARY_FULL(011)`，`missReq.ready=0`，C 被反压到 A 的块回来、主条目腾出后才能进。这就是 `NMshrSubEntry` 对「同块并发 miss 合并能力」的硬上限。

#### 4.3.5 小练习与答案

**练习 1**：为什么 MSHR 的存储用寄存器（`RegInit`）而不是 SRAM？
**答**：因为表很小（`4 主 × 2 子 = 8` 项），且每拍都要并行比较所有主条目的 blockAddr（`blockAddr_Access.map(_ === probe.blockAddr)`）、并行读 `subentry_valid` 计数。SRAM 是按地址串行访问的，无法一拍并行扫所有项；寄存器阵列支持并行读，适合这种小规模全表查找。代价是面积，所以表不能做大。

**练习 2**：`mshrStatus_st0`（组合）和 `mshrStatus_st1_r`（寄存器）为什么要有两套？
**答**：probe 在 st0 拍做块地址比较，但「分配写」与 `missReq` 握手发生在 st1 拍（中间隔了 `MSHR_st1` 流水寄存器）。`mshrStatus_st0` 给 DCache 在 st0 判断「coreReq 能不能收」（满了就别收新请求进来 probe）；`mshrStatus_st1_r/_w` 给 st1 的 `missReq.ready` 用。两套分别匹配两级流水的时序，避免「表已更新但状态还没跟上」的错配（代码注释 [L1MSHR.scala:L271-L273](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1MSHR.scala#L271-L273) 正是解释这点）。

**练习 3**：`a_source` 和 `d_source` 里那 2 位主条目号，本质作用是什么？
**答**：它是「请求—响应配对的钥匙」。primary miss 时 MSHR 把主条目编号放进 `a_source` 发给 L2；L2 回包时原样放进 `d_source` 回送。MSHR 从 `d_source` 取出这 2 位就能直接定位是哪个主条目等的数据（`entryMatchMissRsp := io.missRspIn.bits.instrId`），无需再逐项比较 blockAddr。这比 ICacheMSHR 用 `EntryIdx` 是同一思想的不同实现。

---

## 5. 综合实践

把本讲三个模块串起来，做一次**端到端的「读 miss 全流程」源码跟踪**。目标：用一个具体的读 miss，把 L1Interfaces 的字段、L1TagAccess 的命中/替换、L1MSHR 的 primary/secondary 串成一条链。

**任务**：假设 DCache 收到一个 `DCacheCoreReq`（读、blockAddr=`B`、setIdx=`S`、tag=`T`），且这是一次 miss（组 `S` 的两路都没有 tag=`T`），且组 `S` 两路都已有效（需要替换）。

请按顺序回答并定位源码：

1. **接口层**：这个请求的 `blockAddr` 由 `tag` 和 `setIdx` 怎么拼成？参考 [L1Interfaces.scala:L34-L45](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1Interfaces.scala#L34-L45) 与 DCache 接线 [DCache.scala:L275-L276](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/DCache/DCache.scala#L275-L276)（`probe.bits.blockAddr := Cat(tag, setIdx)`）。
2. **tag 层**：probe 用 `setIdx=S` 读 tag SRAM，`tagChecker` 比较后发现两路都不命中 → `hit_st1=false`。参考 [L1TagAccess.scala:L380-L410](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1TagAccess.scala#L380-L410)。
3. **MSHR 层**：DCache 把 `readMiss_st1` 连到 `MshrAccess.io.missReq.valid`（[DCache.scala:L319](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/DCache/DCache.scala#L319)）。MSHR probe `B` 发现表里没有 → primary miss，分配主条目 `e`，`a_source=e`。参考 [L1MSHR.scala:L187-L201](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1MSHR.scala#L187-L201)、[L1MSHR.scala:L309](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1MSHR.scala#L309)。
4. **替换层**：memRsp 回来要填入时，`ReplacementUnit` 因组已满选 LRU 受害路；若该路 `way_dirty=1` 则 `needReplace` 拉高，DCache 先把它写回 L2 再填新块。参考 [L1TagAccess.scala:L347-L379](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1TagAccess.scala#L347-L379)、[L1TagAccess.scala:L293-L295](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1TagAccess.scala#L293-L295)。
5. **回包层**：L2 回包 `d_source[9:8]=e`，MSHR `entryMatchMissRsp=e`，经 `missRspOut` 把 targetInfo 交回 DCache，DCache 从回填的 cacheline 取出正确字节经 `DCacheCoreRsp` 回 LSU。参考 [L1MSHR.scala:L312-L339](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1MSHR.scala#L312-L339)、[L1Interfaces.scala:L47-L52](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1Interfaces.scala#L47-L52)。

**交付物**：画一张时序图，横轴是时钟周期，画出 `probe`、`entryMatchProbe`、`missReq.fire`、`a_source`、L2 往返、`d_source`、`missRspOut`、`needReplace` 这几条信号在本场景下的变化。如果某些周期的精确取值无法从源码静态确定，标注「待本地验证」。

---

## 6. 本讲小结

- **L1Interfaces** 定义了 DCache 的对外信封：core 侧 `DCacheCoreReq/Rsp` 按 `Vec(NLanes)` 打包逐 lane 地址；mem 侧 `a_source/d_source` 是 13 位（`3 op + 2 MSHR 主条目号 + 8 setIdx`），是请求—响应配对的钥匙。
- 一个 32 位地址切成 `tag(17) | setIdx(8) | blockOffset(5) | wordOffset(2)`；块地址 blockAddr = `tag|setIdx`（25 位）。
- **L1TagAccess** 是组相联目录：tag SRAM + `tagChecker`（命中比较）+ `way_valid`/`way_dirty`（有效/脏位）+ `timeAccess`（LRU）+ `dirtyMaskAccess`（字节级脏图）。DCache 版（`readOnly=false`）额外有 dirty 追踪、`needReplace`、`hasDirty` 扫描；ICache 版精简、用伪随机替换。
- **L1MSHR** 是「主条目 × 子条目」二维表，用寄存器实现以支持并行比较。primary miss 新建主条目并发 L2；secondary miss 只挂子条目不发包；块回来按子条目数展成多拍 `missRspOut`，全部归还后才释放主条目。
- MSHR 用 3 位 `mshrStatus` 状态机（PRIMARY/SECONDARY × AVAIL/FULL + SECONDARY_FULL_RETURN）控制 `missReq.ready`；st0（组合）与 st1（寄存）两套状态分别服务 coreReq 反压与 missReq 握手。
- 两个 MSHR 文件易混淆：`L1MSHR.scala`（DCache，`probe`/`instrId`）vs `ICache/ICacheMSHR.scala`（ICache，`miss2mem`/`EntryIdx`）。

---

## 7. 下一步学习建议

- **u6-l4 共享内存 SharedMemory**：从 DCache 转向片上共享内存，看 bank 化 SRAM 与 `BankConflictArbiter` 如何处理另一类「多 lane 同时访问」的冲突——与本讲的 lane/地址主题呼应。
- **u6-l5 L1Cache2L2 仲裁与 L2 缓存**：本讲的 `a_source/d_source` 在离开 DCache 后，会经 `L1Cache2L2Arbiter`（ICache/DCache 仲裁）进入 L2 Scheduler。下一讲看这 13 位 source 如何在 L2 一侧被进一步贴标/剥标（承接 u2-l2 的 source 路由）。
- **重读 u6-l1 / u6-l2**：现在你已读懂底层零件，回头再看 ICache 的「回填+重放」与 DCache 的「写回/写不分配」会更有体感——尤其是 DCache 为什么 miss 数据直接递回 core（靠 MSHR 的 `missRspOut.targetInfo`），而 ICache 靠重放命中。
- **想动手的读者**：在 `sim-verilator` 里跑一个会触发 DCache miss 的测试用例（如 `vecadd`），用 `--dump-mem` 核对结果；进一步可尝试打开 `cache_spike_info`（`SPIKE_OUTPUT`）观察每次访存的 PC 与地址，对照本讲的地址切分手工验证。
