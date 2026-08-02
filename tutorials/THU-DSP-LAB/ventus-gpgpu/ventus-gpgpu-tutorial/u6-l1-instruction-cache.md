# L1 指令缓存

> 本讲 id：`u6-l1`　依赖：`u4-l1`（SM 流水线总体与取指）
> 阶段：advanced　主文件：`ventus/src/L1Cache/ICache/`

## 1. 本讲目标

学完本讲，你应当能够：

1. 说清楚 Ventus L1 指令缓存（InstructionCache，下称 ICache）**只读、非阻塞**这两条设计原则在源码里是怎么落地的。
2. 画出 ICache 对外的四组接口 `coreReq / coreRsp`（面向 SM 取指）与 `memReq / memRsp`（面向 L2）的交互时序，并解释 2 位 `status` 状态码的四种取值。
3. 解释一次取指如何在两拍命中流水线里读 tag、选 way、切 blockOffset，把 `num_fetch=2` 条指令送回 SM。
4. 彻底读懂 `ICacheMSHR` 的**主条目 / 子条目（primary / secondary miss）**结构，理解它如何把“同一条 cacheline 的多个 miss”合并成一次 L2 请求，又如何在不阻塞取指的前提下把缺失块回填。
5. 看懂 `ICacheParameters` 与 `HasL1CacheParameters` 如何共同决定缓存容量、地址拆分与 MSHR 规模。

## 2. 前置知识

阅读本讲前，你需要先具备以下概念（在 u1、u2、u4 已建立）：

- **warp / SM / 取指**：每个 SM 内有 `num_warp`（默认 8）个 warp，每个 warp 由 `num_thread`（默认 32）个线程组成；`warp_scheduler` 每拍选一个就绪 warp 发起取指，一次取 `num_fetch=2` 条指令。这部分见 u4-l1。
- **cache 基本结构**：cache 用 *set（组）× way（路）* 组织，一个 cacheline（块）是和下一级存储交换的最小单位。命中（hit）指要找的数据在 cache 里，缺失（miss）指不在。
- **MSHR（Miss Status Holding Register）**：一种记录“在途 miss”的硬件表。cache 发出缺失请求后，不等它返回就可以继续服务别的请求；MSHR 负责记住“谁缺了、缺哪块、返回后要交给谁”。
- **非阻塞 cache（non-blocking cache）**：miss 时不会把整个 cache 卡死，而是允许后续请求继续进入。这正是 GPU 隐藏访存延迟的关键。
- **Decoupled / Valid 握手**：Chisel 里 `DecoupledIO` 表示带 `valid/ready/fire` 的握手接口；`ValidIO` 只有 `valid/bits`，无反压。

> 关键直觉：ICache **不把 miss 的数据直接递回 SM**。它只是告诉 SM“这次 miss 了、我已受理”，然后由 `warp_scheduler`（u4-l1）让该 warp 重放（replay）PC 并切到别的 warp；与此同时 ICache 在后台经 MSHR 把缺失块从 L2 取回来写进数据 SRAM。等该 warp 下次再取同一个 PC，就会命中。理解这一点，后面所有逻辑就顺了。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [ventus/src/L1Cache/ICache/ICache.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/ICache/ICache.scala) | ICache 顶层模块 `InstructionCache`，定义四组接口 Bundle、例化 tag/data/MSHR 子模块、实现命中流水线与 miss 处理。本讲的绝对核心。 |
| [ventus/src/L1Cache/ICache/ICacheMSHR.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/ICache/ICacheMSHR.scala) | ICache 专用的 MSHR 实现 `class MSHR`，主/子条目结构、primary/secondary miss 判定、向 L2 发请求与回填。 |
| [ventus/src/L1Cache/ICache/ICacheParameters.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/ICache/ICacheParameters.scala) | `ICacheParameters` case class 与 `HasICacheParameter` trait，经 CDE 配置系统覆盖 set/way/MSHR 规模。 |
| [ventus/src/L1Cache/L1CacheParameters.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1CacheParameters.scala) | `HasL1CacheParameters`：定义容量、地址拆分（tag/setIdx/offset）与所有 `get_*` 地址解码函数。 |
| [ventus/src/L1Cache/L1TagAccess.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1TagAccess.scala) | ICache 版 tag 阵列 `L1TagAccess_ICache`、命中比较器 `tagChecker`、替换单元 `ReplacementUnit_ICache`。 |
| [ventus/src/top/GPGPU_top.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala) | `SM_wrapper` 在这里例化 `InstructionCache` 并把它的四组接口分别接到 `pipe`（取指）与 `L1Cache2L2Arbiter`（去 L2）。 |

---

## 4. 核心概念与源码讲解

### 4.1 InstructionCache：只读非阻塞缓存的全貌与四组接口

#### 4.1.1 概念说明

ICache 夹在 SM 取指前端（`warp_scheduler`）与 L2 缓存之间，只做一件事：**把 warp 要取的指令字节尽快喂给前端**。它有两个鲜明特征：

- **只读（read-only）**：指令不会被写回，因此 ICache 不需要 DCache 那套 dirty 位、写回、写穿逻辑；cacheline 只在“从 L2 取回缺失块”时被写入，替换时直接丢弃旧块即可。源码里 ICache 用的是简化版 tag 阵列 `L1TagAccess_ICache`（无 dirty）。
- **非阻塞（non-blocking）**：miss 发生时，ICache 不会停摆，而是受理 miss、转头继续接其它 warp 的取指请求。这依赖两点：(a) `coreReq.ready` 恒为真，每拍都能收请求；(b) MSHR 在后台追踪在途 miss。

`InstructionCache` 模块对外暴露的四组接口定义在 `ICacheExtInf` 里：`coreReq/coreRsp` 面向 SM（“core”=SM 取指端），`memReq/memRsp` 面向 L2（“mem”=下一级存储）。另外还有 `externalFlushPipe`（分支/冲刷时按 warpid 作废流水线中的请求）、`invalidate`（全局无效化整块 cache），以及在 `MMU_ENABLED` 时才出现的 TLB 接口。

#### 4.1.2 核心流程

整体数据流可以概括为两条通路：

```
        ┌────────────── 取指命中通路（HIT，2 拍） ──────────────┐
coreReq → tagAccess 读 tag ─ st1 比较命中 ─ 选 way/切 blockOffset ─ st2 → coreRsp(data, status=HIT)
        └──────────────────────────────────────────────────────┘

        ┌────────────── 缺失回填通路（MISS，异步） ─────────────┐
coreReq → st1 判定 miss → MSHR.missReq（记录请求者） ──┐
                         MSHR.miss2mem → memReq → L2  │  （core 收到 status=MISS，重放 PC）
                         L2 → memRsp → memRsp_Q ──→ MSHR.missRspIn
                         MSHR.missRspOut → 写 tagAccess + 写 dataAccess （回填）
                                                              ┘
```

关键时序点：

1. **st0（coreReq fire 当拍）**：用地址里的 `setIdx` 同时发起 tag 阵列读和数据 SRAM 读。
2. **st1**：tag 比较出命中与否；若命中，用命中的 waymask 从数据 SRAM 读出的多路结果里选出正确的一路，再按 blockOffset 移位取到需要的 2 条指令；若缺失，向 MSHR 提交 miss 请求。
3. **st2**：命中时输出 `coreRsp`（数据 + status=HIT）；缺失时同样输出 `coreRsp`，但 status 标记为 MISS，数据被前端忽略。
4. miss 回填由 `memRsp` 异步驱动，与取指通路解耦。

ICache 还有一个重要的“同一 warp 短时间内重复到达”的 **OrderViolation** 检测：因为 `coreReq.ready` 恒真，同一个 warp 的 miss 可能在回填完成前被重放并发进来，ICache 通过比对最近两拍的 warpid 与 miss 状态，把这种重复请求标记为“未受理（status=11/10）”，避免对同一 miss 重复计数。

#### 4.1.3 源码精读

**四组接口 Bundle。** 请求/响应的字段在 `ICache.scala` 顶部定义。core 侧请求带地址、有效指令掩码和 warpid：

[ventus/src/L1Cache/ICache/ICache.scala:24-30](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/ICache/ICache.scala#L24-L30) —— `ICachePipeReq`：`addr / mask(num_fetch) / warpid`，`num_fetch=2` 决定一次取 2 条指令。

core 侧响应最关键的是 2 位 `status`，其编码在注释里写得很清楚：

[ventus/src/L1Cache/ICache/ICache.scala:34-47](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/ICache/ICache.scala#L34-L47) —— `ICachePipeRsp` 与状态码表。整理如下：

| status（高位在前） | 含义 | 前端动作 |
| --- | --- | --- |
| `00` | HIT 命中 | `data` 有效，送入 ibuffer |
| `01` | MISS accepted（MSHR 已受理） | 忽略 data，warp 重放 PC |
| `11` | MISS unaccepted（MSHR 满/冲突） | 忽略 data，下拍再试 |
| `10` | invalidate（flush 作废本次请求） | 忽略 data，作废 |

mem 侧（去 L2）的请求与响应：

[ventus/src/L1Cache/ICache/ICache.scala:48-61](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/ICache/ICache.scala#L48-L61) —— `ICacheMemRsp`（`d_source/d_addr/d_data`，`d_data` 是整个 `BlockWords=32` 个字）与 `ICacheMemReq_p`（`a_source/a_addr`）。

`a_source / d_source` 宽度为 `WIdBits + log2Up(NMshrEntry)`，低 `log2Up(NMshrEntry)` 位是 **MSHR 条目号**（用于回填时找回是哪个在途 miss），高 `WIdBits` 位为 warpid 预留。这个 source 编码贯穿 ICache↔L2，是请求—响应配对的钥匙。

四组接口最终在 `ICacheExtInf` 里打包成一个 Bundle：

[ventus/src/L1Cache/ICache/ICache.scala:63-72](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/ICache/ICache.scala#L63-L72) —— 注意方向：`coreReq/memRsp/externalFlushPipe` 是 `Flipped`（输入），`coreRsp/memReq` 是输出；TLB 接口仅在 `MMU_ENABLED` 时存在。

**子模块例化。** `InstructionCache` 例化了四个零件：

[ventus/src/L1Cache/ICache/ICache.scala:79-100](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/ICache/ICache.scala#L79-L100) —— `tagAccess`（ICache 版 tag 阵列）、`dataAccess`（数据 SRAM，按 `NSets×NWays` 组织，每条目 `BlockBits` 位）、`mshrAccess`（缺失状态表）、`memRsp_Q`（深度 2、pipe 的响应队列，吸收 L2 抖动）。

**coreReq 永远就绪 = 非阻塞的根。**

[ventus/src/L1Cache/ICache/ICache.scala:262](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/ICache/ICache.scala#L262) —— `io.coreReq.ready := true.B`。这意味着每个时钟周期都能接受一个取指请求，无论上一次是否 miss。miss 的后果由 status 码反馈给前端，而不是靠反压。

**SM_wrapper 如何接线。** 这四组接口在 `SM_wrapper` 里被分别接到 `pipe` 与 `L1Cache2L2Arbiter`，是理解“ICache 在系统里位置”的最佳入口：

[ventus/src/top/GPGPU_top.scala:369-412](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L369-L412) —— 要点：`coreReq/coreRsp/externalFlushPipe` 接到 `pipe`；`memReq` 接到仲裁器输入端（`a_opcode := 4.U` 即 TileLink Get，`a_source` 直接来自 ICache）；`memRsp` 接到仲裁器输出端，把 L2 返回的 `d_source/d_addr/d_data` 喂回 ICache。ICache 与 DCache 各占仲裁器的一个输入端口（ICache 是端口 0）。

#### 4.1.4 代码实践

**实践目标**：从外部接线确认 ICache“只读、非阻塞”两条特征。

**操作步骤**：
1. 打开 `ventus/src/top/GPGPU_top.scala` 第 369–412 行，列出 `InstructionCache` 的 6 组 io 信号各连到了哪里。
2. 在 `ICache.scala` 全文搜索 `dataAccess.io.w`（数据 SRAM 的写端口），确认**唯一的写来源**是 `memRsp_Q.io.deq.fire`（即 L2 回填），没有任何来自 core 侧的写——这印证了“只读”。
3. 搜索 `io.coreReq.ready`，确认它恒为 `true.B`——这印证了“非阻塞”。

**需要观察的现象**：数据 SRAM 写端口只对应回填路径；core 侧没有任何信号能写数据 SRAM。

**预期结果**：你会清楚地看到，core 侧只有“读请求/读响应”，写只发生在 L2 回填时。

#### 4.1.5 小练习与答案

**练习 1**：为什么 ICache 可以用不带 dirty 位的 `L1TagAccess_ICache`，而 DCache 必须用完整的 `L1TagAccess`（带 dirty、带写回）？
**答**：因为 ICache 只读，cacheline 内容永不被 core 修改，替换时直接丢弃即可，不存在“脏块需要写回 L2”的问题；DCache 要处理 store 命中，必须记录哪些字节被改过（dirty），替换或无效化时才能把脏数据写回。

**练习 2**：`io.coreReq.ready := true.B` 会不会导致同一 warp 的同一 miss 被重复提交给 MSHR？
**答**：会存在这种风险，所以 ICache 用 `OrderViolation` 机制（见 4.1.2 与 4.2.3）比对最近两拍的 warpid 与 miss 状态，把短时间内的重复请求标记为 status `11/10`（未受理/作废），从而避免对同一 miss 在 MSHR 里重复计数。

---

### 4.2 命中流水线：tag 访问、数据读取与 coreRsp 状态码

#### 4.2.1 概念说明

“命中通路”回答的是：一个 coreReq 进来，两拍之后如何把正确的 2 条指令（`num_fetch=2`）挑出来送回 SM。它涉及三件事：

1. **tag 比较**：用地址里的 setIdx 读 tag 阵列，把读出的各路 tag 与地址里的 tag 比较，得到命中标志和命中路（waymask）。
2. **数据选择**：用 setIdx 同时读数据 SRAM（一次读出该 set 的所有路），再用命中 waymask 选出命中路的数据；最后按 blockOffset 移位取到本次需要的字。
3. **状态码生成**：把命中/缺失/受理与否编码成 2 位 status 送给前端。

#### 4.2.2 核心流程

```
st0 (coreReq.fire):
  tagAccess.r.req.setIdx  = get_setIdx(addr)     // 读 tag
  dataAccess.r.req.setIdx = get_setIdx(addr)     // 同时读数据
st1:
  hit     = tagAccess.io.hit_st1 & coreReqFire_st1
  wayidx  = OHToUInt(tagAccess.io.waymaskHit_st1)
  data    = dataAccess.r.resp(wayidx)            // 选命中路
  data'   = data >> (blockOffset << 5)            // 移到目标字
st2:
  coreRsp.valid = coreReqFire_st2
  coreRsp.data  = data'      (命中时有效)
  coreRsp.status= HIT(00) / MISS(01) / MISS-unaccepted(11) / invalidate(10)
```

地址拆分（32 位，默认配置）为：

\[
\underbrace{\text{Tag}}_{17\text{bit}}\;\underbrace{\text{SetIdx}}_{8\text{bit}}\;\underbrace{\text{BlockOffset}}_{5\text{bit}}\;\underbrace{\text{WordOffset}}_{2\text{bit}}
\]

其中 `BlockOffset`（5 位）选中 32 个字中的一个，`num_fetch=2` 要求地址按 8 字节对齐，所以最低 1 位 BlockOffset 必须为 0（源码有断言保证）。

#### 4.2.3 源码精读

**流水线寄存器与命中/缺失判定。**

[ventus/src/L1Cache/ICache/ICache.scala:107-118](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/ICache/ICache.scala#L107-L118) —— `coreReqFire_st1/st2` 把“coreReq 是否 fire”逐拍打下去；`cacheHit_st1 / cacheMiss_st1` 在 st1 用 `tagAccess.io.hit_st1` 给出命中与缺失；`wayidx_hit_st1` 把 one-hot 的 waymask 转成二进制路号。

**tag 读、数据读同步发起。**

[ventus/src/L1Cache/ICache/ICache.scala:145-150](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/ICache/ICache.scala#L145-L150) —— tag 读用 `io.coreReq.fire` 作 valid，setIdx 来自 `get_setIdx(io.coreReq.bits.addr)`，且当 `ShouldFlushCoreRsp_st0`（该 warp 正被外部冲刷）时抑制读。

[ventus/src/L1Cache/ICache/ICache.scala:179-183](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/ICache/ICache.scala#L179-L183) —— 数据 SRAM 读同样在 st0 用 setIdx 发起；读返回的 `NWays` 路结果经 `dataAccess_data(wayidx_hit_st1)` 选出命中路。

**按 blockOffset 切出 2 条指令。**

[ventus/src/L1Cache/ICache/ICache.scala:184-196](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/ICache/ICache.scala#L184-L196) —— 先断言 `blockOffset_sel_st1` 的低 `log2Ceil(num_fetch)` 位为 0（8 字节对齐）；再把整块数据右移 `(blockOffset << 5)` 位（即按字粒度对齐），取低 `num_fetch*xLen=64` 位作为本次返回的 2 条指令。注意 `data_after_blockOffset_st1` 经过一拍 `RegNext` 到 st2 才输出。

**coreRsp 与 status 编码。**

[ventus/src/L1Cache/ICache/ICache.scala:198-221](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/ICache/ICache.scala#L198-L221) —— 要点：
- `coreRsp.valid := coreReqFire_st2 && !OrderViolation_st2`：每个 fire 过的 coreReq 两拍后都会产生一个 coreRsp，命中和缺失都产生。
- `Status_st1` 用 `Cat(高位, cacheMiss_st1)` 拼出 2 位：低位＝是否 miss，高位＝“miss 且未被 MSHR 受理（`!missReq.fire`）或 OrderViolation”。于是命中→`00`，miss 已受理→`01`，miss 未受理→`11`；若该拍被 flush 则强制为 `10`。
- 高位之所以“看起来多余”，是为了让 st0 的 flush 信号能正确地传递成 `10`，注释对此有说明。

**前端如何解读 status。** 这部分在 `pipe.scala` 里，印证了“miss 由前端重放”的设计：

[ventus/src/pipeline/pipe.scala:153-157](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L153-L157) —— `warp_scheduler` 直接拿 `icache_rsp.status` 作为 `pc_rsp.status`；当 ibuffer 不就绪时，还把 status 强制成 `1.U(2.W)`（即“miss”），这正是 u4-l1 讲过的“ibuffer 满伪装 miss 触发 PC 重放”技巧。

[ventus/src/pipeline/pipe.scala:187](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L187) —— `ibuffer.io.in.valid := io.icache_rsp.valid & !io.icache_rsp.bits.status(0)`：只有 `status(0)=0`（命中）时才把指令喂进 ibuffer；miss 时指令被丢弃，等重放后命中再进。

**ICache 版 tag 阵列与替换。** ICache 用的是 `L1TagAccess_ICache`（不带 dirty），命中比较与 DCache 共用同一个 `tagChecker`：

[ventus/src/L1Cache/L1TagAccess.scala:432-509](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1TagAccess.scala#L432-L509) —— 读 tag、比较、`way_valid` 维护、以及回填时经 `ReplacementUnit_ICache` 选出被替换的路。注意 `tagBodyAccess` 用 `holdRead=true` 保持读数据供 st1 使用；`invalidate` 有效时清空全部 `way_valid`（整块 cache 作废）。

[ventus/src/L1Cache/L1TagAccess.scala:510-522](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1TagAccess.scala#L510-L522) —— `ReplacementUnit_ICache`：set 未满时选第一个空路（`PriorityEncoder`），set 满时用一个轮转寄存器（`victim_1Hidx`）选牺牲路。这是一种朴素的伪 LRU/轮转替换，足够只读 cache 使用。

#### 4.2.4 代码实践

**实践目标**：手工“走”一遍命中通路，把每拍的关键信号值写出来。

**操作步骤**：
1. 假设某个 warp 取指地址 `addr = 0x0000_0840`。先用 4.2.2 的拆分公式算出 `Tag / SetIdx / BlockOffset / WordOffset` 各是多少（提示：`0x840 = 0b1000_0100_0000`）。
2. 在 `L1CacheParameters.scala` 里找到 `get_setIdx / get_blockOffset / get_tag / get_blockAddr` 四个函数，核对你的手算结果。
3. 假设该 set 命中 way 1，跟着源码 4.2.3 的三段（tag 读→数据选路→blockOffset 移位）写出 st0/st1/st2 每个信号的取值。

**需要观察的现象**：`wayidx_hit_st1 = 1`，`data_after_blockOffset_st1` 是整块数据右移 `(blockOffset<<5)` 位后的低 64 位。

**预期结果**：`SetIdx = addr(14,7)`、`BlockOffset = addr(6,2)`、`WordOffset = addr(1,0)`；对 `0x840`，SetIdx 与 BlockOffset 可由读者按位算出（待本地验证具体数值，关键是理解位域而非背数字）。

#### 4.2.5 小练习与答案

**练习 1**：为什么数据 SRAM 的读必须在 st0 和 tag 读**同一拍**发起，而不是等 st1 知道命中路之后再读？
**答**：SRAM 读有固有延迟，若等 st1 比较出命中路才发起读，数据要到 st2/st3 才出来，命中通路会变长。Ventus 选择 st0 把整个 set 的所有路一次性读出，st1 用 waymask 从中选路，从而把命中压缩在 2 拍内。

**练习 2**：`status` 是 2 位，但命中时其实 1 位（miss=0）就够了，为什么用 2 位？
**答**：因为还要区分“miss 已受理（01）”和“miss 未受理/被作废（11/10）”这两种前端处理方式不同的状态；多出的高位承载“是否被受理/是否被 flush”信息。

---

### 4.3 ICacheMSHR：主/子条目与非阻塞 miss 管理

#### 4.3.1 概念说明

`ICacheMSHR.scala` 里的 `class MSHR` 是非阻塞能力的核心。它要解决三个问题：

1. **记录谁缺了**：每个 miss 到来时，记下请求者信息（`targetInfo`：warpid 与块内偏移）和缺失块地址（`blockAddr`）。
2. **合并重复缺失（coalesce）**：如果多个 warp 缺的是**同一条 cacheline**，只向 L2 发**一次**请求；这些请求者挂到同一个条目的不同“子条目”里，块返回后逐个归还。
3. **不阻塞取指**：MSHR 满了或冲突时，用 `missReq.ready=0` 告诉 ICache“这次没受理”，ICache 就回 status `11`，前端下拍重试，整体不卡死。

为此 MSHR 采用经典的 **主条目（entry）× 子条目（subentry）** 二维结构：

- 一个**主条目**对应一条**正在向 L2 请求的 cacheline**（由 `blockAddr` 标识）。
- 一个主条目下有多个**子条目**，每个子条目存一个“也缺这条 cacheline”的请求者的 `targetInfo`。
- 主条目数 `NMshrEntry=4`（默认，最多 4 条不同 cacheline 同时在途），每主条目子条目数 `NMshrSubEntry=4`（最多合并 4 个请求者）。

由此引出两个术语：
- **primary miss（主缺失）**：当前没有任何主条目的 blockAddr 与之匹配 → 分配一个新主条目（子条目 0），并向 L2 发请求。
- **secondary miss（次缺失）**：已有主条目匹配同一 blockAddr → 只在它下面挂一个新子条目，**不再**向 L2 发请求（因为已经在途了）。

#### 4.3.2 核心流程

MSHR 的三维状态（以代码里的结构图为准）：

```
主条目0: has_send2mem? + blockAddr#0 || [子0:targetInfo] [子1:targetInfo] ...
主条目1: has_send2mem? + blockAddr#1 || [子0:targetInfo] [子1:targetInfo] ...
主条目2: ...
主条目3: ...
```

四个端口的协作：

```
missReq   (来自 ICache st1 缺失判定):
  若 blockAddr 未匹配任何有效主条目  → primary miss:
      主条目号 = entryStatus.next (第一个空主条目), 子条目号 = 0
  若匹配到主条目 k                  → secondary miss:
      主条目号 = k, 子条目号 = subentryStatus.next (该主条目下第一个空子条目)
  写入 targetInfo；置子条目有效。

miss2mem  (向 L2 请求缺失块):
  找一个“有效但尚未发送”的主条目 m (has_send2mem(m)=0):
      a_addr = blockAddr(m)<<offset, a_source/instrId = m   ← 把主条目号当作 source
      发出后置 has_send2mem(m)=1。   ← 同一 cacheline 只发一次

missRspIn (L2 回填块到达, d_source 低位 = 主条目号 k):
  选中主条目 k，逐个吐出其子条目的 targetInfo (missRspOut):
      每拍清掉一个子条目；当该主条目最后一个子条目被清掉 → 释放主条目 k，清 has_send2mem(k)。
```

**容量上限**：同时最多 `NMshrEntry=4` 条不同 cacheline 在途，每条最多合并 `NMshrSubEntry=4` 个请求者，因此理论上最多同时跟踪 \(4 \times 4 = 16\) 个未决 miss。

#### 4.3.3 源码精读

**MSHR 的四个端口。**

[ventus/src/L1Cache/ICache/ICacheMSHR.scala:73-82](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/ICache/ICacheMSHR.scala#L73-L82) —— `missReq`（ICache 提交的缺失）、`missRspIn`（L2 回填到达）、`missRspOut`（逐个归还请求者信息）、`miss2mem`（向 L2 发请求）。`tIgen` 参数决定 `targetInfo` 位宽。

**二维存储与结构图。** 源码里有一段 ASCII 结构图，是理解整个模块的最佳导览：

[ventus/src/L1Cache/ICache/ICacheMSHR.scala:84-99](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/ICache/ICacheMSHR.scala#L84-L99) —— `blockAddr_Access`（每主条目一个 blockAddr）+ `targetInfo_Accesss`（每主条目×每子条目一个 targetInfo）。`h_s = has_send2mem`、`s_v = subentry_valid`、`e_v = entry_valid`。

**primary / secondary miss 判定。**

[ventus/src/L1Cache/ICache/ICacheMSHR.scala:101-128](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/ICache/ICacheMSHR.scala#L101-L128) —— `entryMatchMissReq` 是一个 one-hot 向量，标记哪些有效主条目的 blockAddr 与新请求匹配（非 MMU 分支见 L155）；`secondary_miss = entryMatchMissReq.orR`，`primary_miss = !secondary_miss`。`getEntryStatus`（L58-71）是个通用工具：输入一个 valid 位向量，输出 `full / next（第一个空位）/ used（PopCount）`。`subentryStatus` 作用在“被 missRspIn 选中的那个主条目”的子条目向量上，用来找空子条目。

**分配主/子条目号。**

[ventus/src/L1Cache/ICache/ICacheMSHR.scala:131-140](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/ICache/ICacheMSHR.scala#L131-L140) —— `missReq.ready` 在三种情况拉低：(a) 主条目满且是 primary miss；(b) 子条目满且是 secondary miss；(c) `ReqConflictWithRsp`（与正在回填的块冲突）。分配号：primary → 主条目 `entryStatus.next` + 子条目 0；secondary → 主条目 `entryMatchMissReq` + 子条目 `subentryStatus.next`。fire 时写入 `targetInfo`。

**冲突保护 ReqConflictWithRsp。**

[ventus/src/L1Cache/ICache/ICacheMSHR.scala:156-159](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/ICache/ICacheMSHR.scala#L156-L159) —— 非 MMU 版：当一个 missReq 的 blockAddr 与“正在回填（missRspIn.fire 或 missRsqBusy）的块”相同时，拉低 ready，让 ICache 把这次请求当作“未受理”，下拍重试。这避免在回填瞬间出现主/次 miss 状态错乱。

**回填处理 missRspIn / missRspOut。**

[ventus/src/L1Cache/ICache/ICacheMSHR.scala:166-176](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/ICache/ICacheMSHR.scala#L166-L176) —— `missRsqBusy` 用于“一个块有多个子条目、但输出端这拍没空”时把状态保持住，逐拍吐子条目；`missRspOut.bits.targetInfo / blockAddr` 取自被选中主条目的下一个待清子条目。当该主条目只剩最后一个子条目且输出就绪时，`missRsqBusy` 解除。

**子条目有效位维护（释放逻辑）。**

[ventus/src/L1Cache/ICache/ICacheMSHR.scala:179-200](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/ICache/ICacheMSHR.scala#L179-L200) —— 双重 `when/elsewhen`：missReq fire 时按分配号置子条目有效；missRspOut fire 时按 `subentry_next2cancel` 清子条目有效。注释提醒：`when` 与 `elsewhen` 的顺序很重要，因为 `elsewhen` 覆盖了 `when` 的部分情形但不对其操作。

**blockAddr 写入（仅 primary miss）。**

[ventus/src/L1Cache/ICache/ICacheMSHR.scala:202-205](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/ICache/ICacheMSHR.scala#L202-L205) —— 只有 primary miss 才写 blockAddr（因为 secondary miss 复用了已有主条目的 blockAddr）。

**向 L2 发请求 miss2mem（has_send2mem 保证只发一次）。**

[ventus/src/L1Cache/ICache/ICacheMSHR.scala:207-222](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/ICache/ICacheMSHR.scala#L207-L222) —— `hasSendStatus` 选出一个“有效但 `has_send2mem=0`”的主条目 `m`；`miss2mem.valid` 成立时把 `blockAddr(m)` 与 `instrId=m`（=主条目号，作为 a_source 低位）送出；fire 后置 `has_send2mem(m)=1`。当某主条目最后一个子条目随 missRspOut 清掉时，`has_send2mem` 复位，该主条目重新可用。**正是 `has_send2mem` 这一位，把同一 cacheline 的多次 secondary miss 合并成了唯一一次 L2 请求。**

**ICache 顶层如何把 MSHR 接进命中/缺失通路。** 回到 `ICache.scala`：

[ventus/src/L1Cache/ICache/ICache.scala:161-171](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/ICache/ICache.scala#L161-L171) —— st1 的 `cacheMiss_st1` 驱动 `mshrAccess.io.missReq`，`targetInfo = Cat(warpid, offsets)`、`blockAddr = get_blockAddr(addr)`；L2 回填经 `memRsp_Q` 进 `missRspIn`，`EntryIdx` 取自 `d_source` 的低位（=主条目号）。

[ventus/src/L1Cache/ICache/ICache.scala:246-256](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/ICache/ICache.scala#L246-L256) —— 非 MMU 路径：`miss2mem` 直接连到对外 `memReq`，`a_addr = blockAddr << offset`、`a_source = instrId`（主条目号）。

[ventus/src/L1Cache/ICache/ICache.scala:152-153](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/ICache/ICache.scala#L152-L153) 与 [ventus/src/L1Cache/ICache/ICache.scala:174-176](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/ICache/ICache.scala#L174-L176) —— 回填时（`memRsp_Q.deq.fire`），用 `missRspOut.bits.blockAddr` 同时**写 tag 阵列**（写命中块地址的 tag，路号由替换单元给出）和**写数据 SRAM**（把 `d_data` 写到该 set 的替换路）。至此缺失块被写回 cache，等该 warp 重放取指时即可命中。

> 小提示：注意 ICache 的 `targetInfo` 在响应侧并未被消费——回填后真正的“归还”是靠把块写进 SRAM、让前端重放命中实现的（`missRspOut.ready := true.B` 且代码注释里有一个“将来可能把 MSHR 信息送给 core”的 TODO）。这是 ICache 与 DCache（u6-l2，miss 数据直接递回）的关键区别。

#### 4.3.4 代码实践

**实践目标**：跟踪一次完整的 miss，画出各阶段信号，体会“非阻塞 + 合并”。

**操作步骤**（源码阅读型实践）：
1. 假设 warp0 取 PC `0x1000`，ICache 缺失（该 set 的两条路都不命中）。请按以下顺序在源码里定位信号：
   - st1：`cacheMiss_st1=1` → `mshrAccess.io.missReq` 的 `blockAddr / targetInfo` 各是什么？（见 ICache.scala L161-164）
   - MSHR 内：这是 primary miss（`entryMatchMissReq=0`），分配主条目 `entryStatus.next`（假设为 0），子条目 0。（见 ICacheMSHR.scala L135-140）
   - `miss2mem`：选中主条目 0，`has_send2mem(0)=0` → 发出 `a_source=0`、`a_addr=blockAddr<<7`，置 `has_send2mem(0)=1`。（见 L207-222）
2. 在 miss 未返回期间，假设 warp1 取 PC `0x1040`（**同一条 cacheline**，因为 0x1000 与 0x1040 的 `blockAddr` 相同）。这会是一次 secondary miss：`entryMatchMissReq` 命中主条目 0，于是只把 warp1 的 targetInfo 挂到主条目 0 的子条目 1，**不**再发 miss2mem。请确认这一点。
3. L2 回填：`memRsp.d_source=0` → `missRspIn.EntryIdx=0` → missRspOut 先吐子条目0（warp0）再吐子条目1（warp1）；同时回填写 tag+data SRAM。最后主条目 0 释放，`has_send2mem(0)` 复位。
4. 画出 5 个阶段（请求→primary→secondary→回填→释放）的简表，标出每阶段有效的信号。

**需要观察的现象**：secondary miss 不会产生新的 `miss2mem`；回填时 `d_source` 的低位精确指回当初的主条目号。

**预期结果**：你会清楚看到“一条 cacheline 的 N 个 miss → 1 次 L2 请求 → N 次子条目归还 → 1 次主条目释放”这一合并过程。

> 待本地验证：以上为源码阅读推导。若要实测波形，可按 u1-l4 用 sim-verilator 跑一个程序，在 ICache 相关信号上 dump 波形（如 `mshrAccess` 的 valid_list、`has_send2mem`、`miss2mem.valid`），观察一次真实 miss 的各阶段。

#### 4.3.5 小练习与答案

**练习 1**：如果 4 个主条目全部被占用，又来了一个 primary miss，会发生什么？
**答**：`entryStatus.io.full=1` → `missReq.ready=0`（见 L132-133）→ ICache 该拍 `missReq.fire=0` → status 高位被置 1，回给前端 status `11`（MISS unaccepted）→ 前端下拍重试，直到有主条目被释放。

**练习 2**：secondary miss 为什么不需要（也不能）再发一次 `miss2mem`？
**答**：因为匹配的主条目已经 `has_send2mem=1`，L2 请求已经在途；再发一次只会重复占用带宽。secondary miss 只需把自己的 targetInfo 记到该主条目的子条目里，等同一个块返回时一起被归还。

**练习 3**：`missRspIn.bits.EntryIdx` 为什么直接取自 `d_source` 的低位，而不是拿 blockAddr 去全表比较？
**答**：因为发出 miss2mem 时，`a_source` 低位就被设成了主条目号，L2（经仲裁器）原样把它作为 `d_source` 回送。直接用 source 取号是 O(1) 的，比遍历比较 blockAddr 更快，且天然保证配对正确。

---

### 4.4 ICacheParameters 与缓存参数、地址解码

#### 4.4.1 概念说明

ICache 的容量、结构、MSHR 规模并非写死，而是由两层参数共同决定：

- **`ICacheParameters` case class**（经 CDE 配置系统注入，见 u2-l3）：覆盖 set/way/MSHR 主条目数/子条目数。
- **`HasL1CacheParameters` trait**：在上面这些原始数值之上，推导出地址各位宽度（`TagBits/SetIdxBits/...`）、缓存总容量，以及所有 `get_*` 地址解码函数。

掌握这一层，你才能回答“ICache 到底多大”“一个地址怎么拆”这类问题。

#### 4.4.2 核心流程

默认配置下（`MMU_ENABLED=false`）：

| 参数 | 取值 | 出处 |
| --- | --- | --- |
| `NSets` | 256（取自 `dcache_NSets`） | parameters.scala L71 |
| `NWays` | 2（取自 `dcache_NWays`） | parameters.scala L73 |
| `BlockWords` | 32（取自 `dcache_BlockWords`） | parameters.scala L75 |
| `NMshrEntry` | 4 | ICacheParameters.scala L24 |
| `NMshrSubEntry` | 4 | ICacheParameters.scala L25 |
| `num_fetch` | 2 | parameters.scala L38 |

由此可算出 ICache 容量：

\[
\text{容量} = \text{NSets}\times\text{NWays}\times\text{BlockWords}\times 4\text{B}
= 256\times 2\times 32\times 4 = 65\,536\text{B} = 64\text{KiB}
\]

每个 cacheline（块）大小：

\[
\text{BlockBytes} = \text{BlockWords}\times 4 = 128\text{B},\qquad \text{BlockBits}=1024\text{bit}
\]

地址位宽拆分（32 位字长）：

\[
\text{TagBits}=32-(\underbrace{\log_2 256}_{8}+\underbrace{\log_2 32}_{5}+\underbrace{\log_2 4}_{2})=17
\]

MSHR 在途容量上限：

\[
\text{未决 miss 上限} = \text{NMshrEntry}\times\text{NMshrSubEntry}=4\times 4=16
\]

#### 4.4.3 源码精读

**ICacheParameters 与 trait 覆盖。**

[ventus/src/L1Cache/ICache/ICacheParameters.scala:17-40](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/ICache/ICacheParameters.scala#L17-L40) —— `ICacheParamsKey` 是 CDE 的 `Field`，`ICacheParameters` 给出默认值（`nSets=dcache_NSets` 等）；`HasICacheParameter` 用 `override def` 把这些值覆盖到 `HasL1CacheParameters` 的同名函数上。注意 ICache 的 set/way **默认沿用 DCache 的尺寸**（`dcache_NSets/dcache_NWays`），所以默认下两级 L1 cache 物理规格相同。

**容量与地址宽度的推导。**

[ventus/src/L1Cache/L1CacheParameters.scala:30-51](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1CacheParameters.scala#L30-L51) —— 关键派生：`BlockBytes=BlockWords*4`、`BlockBits=BlockBytes*8`、`SetIdxBits=log2Up(NSets)`、`TagBits=WordLength-(SetIdxBits+BlockOffsetBits+WordOffsetBits)`、`bABits=TagBits+SetIdxBits`（blockAddr 位宽）、`WayIdxBits=log2Up(NWays)`。

**地址解码函数（贯穿全讲）。**

[ventus/src/L1Cache/L1CacheParameters.scala:54-66](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1CacheParameters.scala#L54-L66) —— 这五个函数是本讲所有“拆地址”的来源：
- `get_tag(addr)`：取最高 `TagBits` 位。
- `get_setIdx(addr)`：按地址宽度（32 位 or bABits）取中间的 setIdx 位。
- `get_blockOffset(addr)`：`addr(BlockOffsetBits+WordOffsetBits-1, WordOffsetBits)` —— 字号。
- `get_offsets(addr)`：blockOffset + wordOffset，存进 `targetInfo` 的块内偏移。
- `get_blockAddr(addr)`：`tag + setIdx`，即 MSHR 用来标识缺失 cacheline 的 `blockAddr`。

**配置注入。** `MyConfig` 把 `ICacheParamsKey` 等键绑定到具体参数 case class，`SM_wrapper` 里用 `(new MyConfig).toInstance` 作为隐式 `Parameters` 传给 `InstructionCache`：

[ventus/src/L1Cache/L1CacheParameters.scala:21-27](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1CacheParameters.scala#L21-L27) —— `MyConfig` 同时绑定 DCache/ICache/ShareMem/RVG 四组参数键。

#### 4.4.4 代码实践

**实践目标**：动手改一个参数，观察对生成 RTL 的影响。

**操作步骤**：
1. 在 `ventus/src/L1Cache/ICache/ICacheParameters.scala` 把 `nMshrEntry` 默认值从 `4` 改成 `8`（即增大在途 miss 容量）。
2. 按 u1-l2 执行 `make verilog` 重新生成 `GPGPU_top.v`。
3. 在生成的 Verilog 里搜索 ICache 的 MSHR 相关寄存器（如含 `has_send2mem`、`subentry_valid` 的数组），观察其深度/位宽是否随 `nMshrEntry` 变化。

**需要观察的现象**：MSHR 主条目相关寄存器阵列的规模随 `nMshrEntry` 增大而增大；`a_source/d_source` 中 MSHR entry 字段位宽（`log2Up(NMshrEntry)`）从 2 变 3。

**预期结果**：增大 `nMshrEntry` 会增加 ICache 可同时追踪的缺失 cacheline 数，硬件代价是更多寄存器与更宽的 source 字段。**注意：改完务必 `git checkout -- ventus/src/` 还原，不要把修改留在源码里。**

> 待本地验证：生成 Verilog 较耗内存（建议 `-Xmx32G`，见 u1-l2）；若不便生成，也可只阅读 Chisel 源码确认逻辑关系。

#### 4.4.5 小练习与答案

**练习 1**：若把 `BlockWords` 从 32 改成 64，`TagBits` 会怎么变？命中率通常会如何变化？
**答**：`BlockOffsetBits=log2 64=6`（原 5），`TagBits=32-(8+6+2)=16`（原 17），变少。块变大通常提升空间局部性、提高命中率，但也会增大单次缺失取数的流量，并减少同等容量下的 set 数或路数。

**练习 2**：为什么 ICache 的 `nSets/nWays` 默认直接复用 `dcache_NSets/dcache_NWays`？
**答**：简化配置、统一 SRAM 规格便于综合时复用同一套存储宏；指令与数据的工作集特性虽不同，但默认配置选择共用尺寸，需要时可通过 CDE 单独覆盖 `ICacheParamsKey`。

---

## 5. 综合实践：端到端跟踪一次 icache miss

把本讲四个模块串起来，完成下面这个贯穿性任务。

**任务背景**：某 SM 上 warp0 和 warp2 先后取指，且两者取指地址落在**同一条 cacheline**；该 cacheline 当前不在 ICache 里。

**要求**：

1. **取指请求阶段**（模块 4.1）：
   - 说明 `pipe.io.icache_req` 如何被接到 `icache.io.coreReq`（addr/warpid/mask）。
   - 写出 warp0 的 coreReq 进入后，`coreReq.ready` 的取值，并解释为何恒真。

2. **命中判定与 miss 提交阶段**（模块 4.2 + 4.3）：
   - 说明 st0/st1 如何读 tag 并判定 `cacheMiss_st1=1`。
   - warp0 的 miss 如何作为 primary miss 进入 MSHR：分配哪个主条目（假设为 0）、`miss2mem` 如何发出（`a_source=0`）、`has_send2mem(0)` 如何被置位。

3. **合并阶段**（模块 4.3）：
   - warp2 随后取指同一 cacheline，说明它被识别为 secondary miss，挂在主条目 0 的子条目 1，且**不再**发 miss2mem。
   - 与此同时，前端因收到 status=`01`（MISS accepted）做了什么？（提示：重放 PC、切到别的 warp，这正是“非阻塞支持多 warp 交替取指”的体现。）

4. **回填与释放阶段**（模块 4.3 + 4.1）：
   - L2 返回 `memRsp.d_source=0`，经 `memRsp_Q` → `missRspIn`（EntryIdx=0）。
   - 说明 `missRspOut` 如何驱动**同时写 tag 阵列与数据 SRAM**（回填到替换路）。
   - 说明子条目 0、1 如何被逐个清掉，主条目 0 如何随最后一个子条目释放、`has_send2mem(0)` 复位。
   - 说明 warp0/warp2 重放取指时为何这次会命中（status=`00`，数据进 ibuffer）。

5. **画图**：把上述 5 个阶段画成一张时序/流程图，横轴为时钟周期（可用相对周期 T0、T1、…），纵轴标注 `coreReq/coreRsp.status/missReq/miss2mem/memRsp/missRspOut/has_send2mem` 等关键信号的变化。

**验收标准**：你能向别人讲清楚“为什么同一 cacheline 的两个 miss 只产生一次 L2 请求”“为什么 miss 不会卡住别的 warp 取指”“回填后前端凭什么能拿到指令”这三件事，本讲就通关了。

> 待本地验证：以上为静态源码推导。要看到真实波形，可参考 u1-l4 在 sim-verilator 下运行一个测试用例并 dump ICache 相关信号；若暂时无法仿真，完成本任务的“画图 + 文字解释”即达到本讲的学习目标。

## 6. 本讲小结

- ICache 是**只读、非阻塞**的 L1 指令缓存：只读意味着无 dirty/写回，替换直接丢；非阻塞的根在于 `coreReq.ready` 恒真，miss 由 2 位 `status` 反馈给前端。
- 四组接口 `coreReq/coreRsp`（面向 SM 取指）与 `memReq/memRsp`（面向 L2）在 `SM_wrapper` 里分别接到 `pipe` 与 `L1Cache2L2Arbiter`；`a_source/d_source` 的低位是 MSHR 主条目号，是请求—回填配对的钥匙。
- 命中通路是 2 拍流水线：st0 用 setIdx 同读 tag 与数据 SRAM，st1 比较命中 + 选路 + 按 blockOffset 移位切出 `num_fetch=2` 条指令，st2 经 `coreRsp` 返回。
- `status` 四种取值（`00` HIT / `01` MISS accepted / `11` MISS unaccepted / `10` invalidate）由 `cacheMiss_st1` 与“是否被 MSHR 受理/是否 flush”拼出；前端只对 `status(0)=0` 的命中结果送 ibuffer。
- `ICacheMSHR` 用**主条目×子条目**二维结构实现 primary/secondary miss 合并：`has_send2mem` 保证同一 cacheline 只发一次 L2 请求，多个请求者挂子条目、回填时逐个归还。
- 参数由 `ICacheParameters`（经 CDE）覆盖 set/way/MSHR 规模，`HasL1CacheParameters` 推导地址位宽与 `get_*` 解码函数；默认为 64KiB、2 路、128B 行、4 主×4 子 MSHR。

## 7. 下一步学习建议

- **u6-l2 L1 数据缓存**：对照学习 DCache。重点看它为何需要 dirty 位、WSHR、以及 DCache 的 MSHR 如何把 miss 数据**直接递回 core**（与 ICache 的“回填+重放”形成对比）。
- **u6-l3 Tag 访问与 MSHR 机制**：更系统地看 `L1TagAccess`（DCache 版，含 dirty/替换）与 `L1MSHR` 的主/子条目状态机，本讲的 `ICacheMSHR` 是其精简兄弟，对照阅读能加深理解。
- **u6-l5 L1Cache2L2 仲裁与 L2 缓存**：继续顺着 `memReq` 往下走，看 ICache/DCache 的请求如何被仲裁并送进 L2 Scheduler，理解 source 字段在更下层互联里的延续。
- **u7-l1 MMU 与 TLB/PTW**：本讲多次出现的 `MMU_ENABLED` 分支（TLBReq/TLBRsp、a_addr 用 paddr、ASID 匹配）将在那里完整展开。
