# LSU 访存单元

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚一条向量/标量访存指令进入 LSU 后，地址是如何被计算、合并（coalesce）并路由到 SharedMemory 或 DCache 的；
- 解释 `MshrTag`、`DCacheCoreReq_np`、`ShareMemCoreReq_np` 三个 Bundle 各自携带什么信息、在请求/响应闭环里扮演什么角色；
- 描述 LSU 内部 MSHR 如何记录“在途请求”、收集分批返回的数据并按 lane 还原出一条完整指令的结果；
- 说明 `fence` 指令如何借助 `ShiftBoard` 等待同 warp 的所有在途访存完成。

本讲是 SM 流水线后端（u5）的访存篇，承接 u5-l1（发射与执行单元总览）。在那里我们看到 Issue 把访存类指令从 `out_LSU` 端口送出；本讲就从这个端口往下走，直到结果经 `LSU2WB` 写回寄存器堆。

## 2. 前置知识

在读源码之前，先建立三个直觉。

**（1）一条向量访存指令 = 32 个独立地址。** Ventus 的一个 warp 有 `num_thread`（默认 32）个线程，一条向量 load/store 指令会让每个 lane 各算出一个地址。这 32 个地址彼此可能相邻（连续访存），也可能毫无规律（gather/scatter）。LSU 的核心职责之一，就是把这 32 个地址“翻译”成 cache 能理解的 cacheline 请求。

**（2）cacheline 是访存的基本单位。** DCache 的一个 cacheline = `dcache_BlockWords` 个 word = 32 word = 128 字节。任何一个字节地址都可以拆成四段：

\[
\text{addr} = \underbrace{\text{tag}}_{17\text{b}}\; \underbrace{\text{setIdx}}_{8\text{b}}\; \underbrace{\text{word-in-block}}_{5\text{b}}\; \underbrace{\text{byte-in-word}}_{2\text{b}}
\]

其中 `tag + setIdx` 唯一确定一条 cacheline。落在同一条 cacheline 里的多个 lane，可以合并成一次 cache 请求——这就是 **coalesce（访存合并）**。

**（3）Ventus 靠地址范围区分片上/片外内存。** 默认不启用 MMU，地址空间被硬切成两块（见 u2-l1）：

- 落在 `[LDS_BASE, LDS_BASE + 128KiB)` 内的地址走片上 **SharedMemory**（`LDS_BASE = 0x70000000`）；
- 其余地址经 **DCache → L2 → AXI → DDR** 访问 global memory。

> 术语提示：MSHR = Miss Status Holding Register，原本指 cache 里记录“未命中且在途”的表项；Ventus 的 LSU 也借用这个名字，用它记录“一条访存指令已发出、但数据还没收齐”的状态，与 cache 内部的 MSHR 是两套东西，不要混淆（cache 的 MSHR 见 u6）。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [ventus/src/pipeline/LSU.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/LSU.scala) | 本讲主文件：请求/响应 Bundle、`AddrCalculate`、`LSUexe`、`LSU2WB`、`ShiftBoard` 全在这里 |
| [ventus/src/pipeline/MSHR.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/MSHR.scala) | `MSHRv2`：LSU 内部记录在途请求、收集返回数据的核心表 |
| [ventus/src/pipeline/pipe.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala) | 把 `LSUexe` 例化进 SM 流水线，连接 Issue、CSR、DCache、SharedMem、写回 |
| [ventus/src/pipeline/scoreboard.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/scoreboard.scala) | `fence` 在此被记分板锁定，等待 LSU 的 `fence_end` 解锁 |
| [ventus/src/top/parameters.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala) | `LDS_BASE`、`lsu_nMshrEntry`、`dcache_BlockWords` 等关键常量 |

---

## 4. 核心概念与源码讲解

### 4.1 LSUexe：访存单元总览

#### 4.1.1 概念说明

`LSUexe` 是 SM 对外的“访存门面”。对上游（Issue）它接收已经收集好操作数的访存指令（`lsu_req`，`vExeData` 类型，含 `in1/in2/in3` 三个源操作数向量 + `mask` + 控制信号 `ctrl`）；对下游它把请求分别发往 DCache 和 SharedMemory，并把收回的数据整理成写回包送给 `LSU2WB`。它还要回答两个全局问题：“这条指令的访存都做完了吗？”（`fence_end`）。

#### 4.1.2 核心流程

`LSUexe` 内部由五个零件串成一条单向流水：

```
Issue.out_LSU
   │  vExeData(基址 in1, 偏移 in2, 写数据 in3, mask, ctrl)
   ▼
[InputFIFO] ──► [AddrCalculate] ──┬── to_dcache  ──► DCache
                                  ├── to_shared  ──► SharedMemory
                                  └── to_mshr(tag)──►
                                                       [MSHRv2(Coalescer)]
   DCache/shared 响应 ──► [rspArbiter] ──► from_dcache ──► MSHRv2
                                                        │ 收齐后 to_pipe(MSHROutput)
                                                        ▼
                                                  [LSU2WB] ──► wb.in_x(2)/in_v(2)
   [ShiftBoard ×num_warp]  统计每 warp 在途数 → fence_end
```

要点：

1. **InputFIFO** 是深度 1 的流水队列，做一拍对齐。
2. **AddrCalculate** 做地址计算、coalesce、路由判定，是本讲重点（4.2）。
3. **rspArbiter** 用一个 2 选 1 仲裁器把 DCache 响应与 SharedMemory 响应（两者都用 `DCacheCoreRsp_np`）合并成一路送给 MSHR。
4. **MSHRv2**（代码里变量名 `Coalscer`）记录每条在途指令“还差哪些 lane 的数据”，收齐后输出（4.4）。
5. **ShiftBoard** 给每个 warp 维护一个在途计数，全部清空时拉高 `fence_end`（4.5）。

#### 4.1.3 源码精读

顶层 IO 与零件例化集中在 `LSUexe`：

[ventus/src/pipeline/LSU.scala:530-547](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/LSU.scala#L530-L547) —— 定义 `lsu_req`（上游指令）、`dcache_req/dcache_rsp`、`shared_req/shared_rsp`、`lsu_rsp`（送写回）、`fence_end`、`flush_dcache` 以及读 CSR 用的 `csr_pds/csr_numw/csr_tid`。

零件连线：

[ventus/src/pipeline/LSU.scala:551-570](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/LSU.scala#L551-L570) —— 注意 `Coalscer`（即 `MSHRv2`）同时接两路输入：`from_addr`（AddrCalculate 申请 MSHR 表项）和 `from_dcache`（经 `rspArbiter` 合并后的响应）；`idx_entry` 是 MSHR 分配出的表项号，回送给 AddrCalculate，写进请求的 `instrId` 字段——这正是请求与响应“对上号”的钥匙。

`pipe.scala` 里 LSU 的接入：

[ventus/src/pipeline/pipe.scala:376-377](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L376-L377) —— 向量 Issue 的 `out_LSU` 接到 `lsu_req`；标量 Issue 的 `out_LSU.ready := false.B`（标量访存也走向量通路）。

[ventus/src/pipeline/pipe.scala:136-139](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L136-L139) —— CSR 把 `lsu_pds/lsu_tid/lsu_numw` 喂给 LSU，供私有内存地址计算使用（4.2 会用到）。

[ventus/src/pipeline/pipe.scala:410-424](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L410-L424) —— `dcache_req/dcache_rsp/shared_req/shared_rsp` 直连 SM 顶层；`lsu_rsp` 经 `LSU2WB` 转成 `wb.in_x(2)` 与 `wb.in_v(2)` 写回。

#### 4.1.4 代码实践

**实践目标**：建立“LSU 是 Issue 与 cache 之间的中间层”的整体印象。

**操作步骤**：

1. 打开 `ventus/src/pipeline/pipe.scala`，定位到 `val lsu=Module(new LSUexe)`。
2. 沿 `issueV.io.out_LSU<>lsu.io.lsu_req`（约 376 行）往下，依次找到 `dcache_req/dcache_rsp/shared_req/shared_rsp` 与 `lsu2wb` 的连接。
3. 对照本节上面的流水示意图，在源码里给每个零件标注一个序号。

**需要观察的现象**：你会发现 LSU 对外只暴露一组 `dcache_req/rsp` 和一组 `shared_req/rsp`，cache 侧并不感知“warp/lane/mask”这些 SIMT 概念——这些都被 LSU 吸收掉了。

**预期结果**：能口述“一条 `vExeData` 进、一组 cacheline 请求出、响应经 MSHR 聚合后变回写回包”的完整路径。

---

### 4.2 AddrCalculate：地址计算、coalesce 与路由

这是 LSU 最复杂、也最值得读的部分。它用一个 6 状态的 FSM，把一条向量访存指令拆解成若干 cacheline 请求。

#### 4.2.1 概念说明

`AddrCalculate` 要同时回答四个问题：

1. **每个 lane 的地址是多少？** 取决于寻址模式（`mop`）：单位步进（strided）、indexed（按向量下标）、还是标量地址。
2. **走 shared 还是 dcache？** 看地址是否落在 LDS 区间。
3. **哪些 lane 可以合并进同一次请求？** 看 tag+setIdx 是否相同（同一 cacheline）。
4. **本次请求送出去后，还剩哪些 lane 没处理？** 更新 mask，下一拍继续。

#### 4.2.2 核心流程

FSM 状态定义：

[ventus/src/pipeline/LSU.scala:128](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/LSU.scala#L128)

```
s_idle ──fire──► s_save ──┬─(全部落在 LDS)──► s_shared ──(循环合并)──► s_idle
                           └─(否则)──────────► s_dcache ──(循环合并)──► s_idle
                                              (atomic aq/rl 时还会经 s_dcache_1/s_dcache_2 拆成最多 3 拍)
```

主循环伪代码（DCache 路径）：

```
保存指令到 reg_save，向 MSHR 申请表项得到 instrId
while (reg_save.mask != 0):
    addr_wire = 第一个活动 lane 的地址          # 用 PriorityEncoder 取锚点
    (tag, setIdx) = 拆 addr_wire 的高位
    本拍请求.activeMask[x] = mask[x] AND (addr[x] 的高位 == tag,setIdx)   # 同 cacheline 才合并
    发 to_dcache（含 instrId, tag, setIdx, perLaneAddr, data, opcode/param）
    mask_next[x] = mask[x] AND NOT(命中本 cacheline)                      # 清掉已处理的 lane
    reg_save.mask := mask_next
# mask 归零，指令处理完毕，回 s_idle
```

**coalesce 的本质**：不是把 32 个地址塞进一个“胖请求”，而是“同一 cacheline 的 lane 共用一次请求、不同 cacheline 的 lane 分多拍依次发”。因此一条跨度很大的向量访存可能拆成多次 cache 请求；而一段连续访存往往只需一两次。

**地址计算公式**（关键代码）：

[ventus/src/pipeline/LSU.scala:146-163](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/LSU.scala#L146-L163) —— 按寻址模式 `mop` 分支：

| `mop` | 模式 | 每 lane 地址 |
|-------|------|--------------|
| 0 | unit-stride（`VLE32_V`） | `in1 + lane*4` |
| 3 | indexed（`VLOXEI32_V`，下标来自 `in2`） | `in1 + in2[lane]` |
| 其他 | strided（`VLSE32_V`，步长来自 `in2`） | `in1 + lane*in2[lane]` |
| 标量 | 非向量 | `in1[0] + in2[0]` |

其中 `is_vls12`（私有内存 load/store 12 位偏移）走另一套用 `csr_pds/csr_numw/csr_tid` 拼地址的公式，用于访问每 warp 的私有数据空间（PDS）。

**路由判定**：

[ventus/src/pipeline/LSU.scala:162-167](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/LSU.scala#L162-L167) —— 关键两行：

```scala
is_shared(x) := !reg_save.mask(x) || ( addr(x) >= LDS_BASE && addr(x) < (LDS_BASE + sharedmemory_maxsize) )
all_shared   := is_shared.asUInt.andR   // 向量时要求所有活动 lane 都在 LDS 区间
```

注意 `!reg_save.mask(x)`：被 mask 关掉的 lane 一律视为“shared”，这样它们不会因为地址落在 global 而把整条指令逼去 dcache。只有当**所有活动 lane** 都落在 LDS 区间，才走 `s_shared`；否则走 `s_dcache`。

**tag/setIdx 提取与合并键**：

[ventus/src/pipeline/LSU.scala:168-176](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/LSU.scala#L168-L176) —— 用 `PriorityEncoder(reg_save.mask)` 取第一个活动 lane 作为锚点，从它的地址拆出 `tag`（高 17 位）和 `setIdx`（中 8 位）；随后 `same_tag(x)` 判断每个 lane 的高位是否与锚点一致。

**字内字节选择 `wordOffset1H`**：

[ventus/src/pipeline/LSU.scala:179-193](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/LSU.scala#L179-L193) —— 按 `mem_whb`（MEM_W/MEM_H/MEM_B，见 [DecodeUnit.scala:54-56](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/DecodeUnit.scala#L54-L56)）生成 4 位 one-hot 的字节使能：全字=`1111`，半字看 `addr[1]` 选 `0011`/`1100`，字节看 `addr[1:0]` 移位。这个 one-hot 码最终告诉 cache“这次要读/写一个 word 里的哪几个字节”。

**mask 更新（coalesce 的“下一拍”）**：

[ventus/src/pipeline/LSU.scala:283-287](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/LSU.scala#L283-L287) —— `mask_next` 把“本次已命中 cacheline”的 lane 从 mask 中扣除；FSM 在 [LSU.scala:321-343](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/LSU.scala#L321-L343) 里据此决定是继续留在 `s_dcache`（还有别的 cacheline 要发）还是回 `s_idle`（mask 归零，完成）。

> 补充：atomic（LR/SC/AMO）指令会强制只取第一个活动 lane（`x === PriorityEncoder(mask)`，见 [LSU.scala:275](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/LSU.scala#L275)），并按 `aq/rl` 位经 `s_dcache_1/s_dcache_2` 把一次原子操作拆成最多 3 个 TileLink 风格的子请求（opcode/param 编码见 [LSU.scala:239-270](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/LSU.scala#L239-L270)）。初学可先跳过这段，抓住普通 load/store 主线即可。

#### 4.2.3 源码精读

`AddrCalculate` 的 IO：

[ventus/src/pipeline/LSU.scala:113-127](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/LSU.scala#L113-L127) —— 注意它的三个输出端口 `to_mshr`（申请 MSHR 表项）、`to_dcache`、`to_shared`，以及从 MSHR 回送的 `idx_entry`。

DCache 请求装配（含 activeMask 与 data）：

[ventus/src/pipeline/LSU.scala:272-282](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/LSU.scala#L272-L282) —— `activeMask` 正是上面说的合并判定结果；`data := reg_save.in3`（写数据来自第三源操作数）。

SharedMemory 请求装配：

[ventus/src/pipeline/LSU.scala:213-224](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/LSU.scala#L213-L224) —— 与 dcache 类似，但 Bundle 不带 tag（sharedmem 不用 tag 查找），只带 `setIdx`、每 lane 的 `blockOffset/wordOffset1H/activeMask`、`data`、`isWrite`。

#### 4.2.4 代码实践

**实践目标**：手工跑一遍一条向量 load 的地址计算与合并。

**操作步骤**：

1. 假设一条 `VLE32_V`（`mop=0`，unit-stride），基址 `in1 = 0x8000_0100`，`mask = 0xFFFFFFFF`（32 个 lane 全活）。
2. 按 [LSU.scala:153-158](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/LSU.scala#L153-L158) 计算：lane 0 → `0x8000_0100`，lane 1 → `0x8000_0104`，…，lane 31 → `0x8000_017C`。
3. 取锚点 lane 0 的地址 `0x8000_0100`，拆出 tag/setIdx。
4. 判断 32 个 lane 是否都落在同一条 cacheline（128 字节 = 32 word）。

**需要观察的现象**：lane 0~31 的地址跨距正好是 128 字节，恰好**压在一条 cacheline 的边界上**——前 0~31 word 中，由于起点的 word-in-block 不同，可能需要 1 次或 2 次 cache 请求。请自己算出锚点的 word-in-block（`addr[6:2]`）来确认。

**预期结果**：写出该指令需要发出几次 `to_dcache` 请求、每次请求的 `activeMask` 覆盖哪些 lane。若锚点 word-in-block=0，则 32 lane 同 cacheline，1 次请求即可；否则需 2 次。**待本地验证**：可在仿真里给 LSU 的 `to_dcache.fire` 打印 `instrId` 与 `activeMask`，数实际发出的次数。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `is_shared(x)` 要把 `!reg_save.mask(x)`（被关闭的 lane）也算作 true？

**答**：路由判定用 `all_shared = AND` 归约。若不把关闭的 lane 算 true，一个被 mask 关掉、且地址恰好落在 global 的 lane 会把 `all_shared` 拉低，导致整条本应走 sharedmem 的指令被误送到 dcache。把关闭 lane 视作 shared，等价于“只看活动 lane 的地址来决定路由”。

**练习 2**：`mop=3`（indexed）和 `mop=0`（unit-stride）在地址公式上的区别是什么？

**答**：unit-stride 用固定的 `lane*4` 作为偏移，因此地址连续、极易 coalesce；indexed 用 `in2[lane]` 作为偏移，每个 lane 的下标各不相同，地址通常不连续、coalesce 效率低，往往拆成很多次 cache 请求。

---

### 4.3 三个关键 Bundle：MshrTag、DCacheCoreReq_np、ShareMemCoreReq_np

#### 4.3.1 概念说明

LSU 在“多 lane、多 cacheline、可能乱序返回”的环境里，需要一套数据结构来：(a) 描述一次发给 cache 的请求；(b) 记住一条指令“还差哪些 lane”。这三个 Bundle 就是这套数据结构。

#### 4.3.2 核心流程

请求侧：

- `AddrCalculate` 把每条指令的“身份信息”打包成 `MshrTag`，经 `to_mshr` 交给 MSHR 登记；
- 同时把 cacheline 请求打包成 `DCacheCoreReq_np`（或 `ShareMemCoreReq_np`），其中携带 MSHR 回送的 `instrId`。

响应侧：

- cache 返回 `DCacheCoreRsp_np`（带相同的 `instrId` + 这一批的 `activeMask` + `data`），MSHR 据 `instrId` 找到对应指令、据 `activeMask` 标记“这批 lane 到了”。

#### 4.3.3 源码精读

**`MshrTag`** —— 一条访存指令的身份卡：

[ventus/src/pipeline/LSU.scala:82-93](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/LSU.scala#L82-L93) —— 含 `warp_id`、写回目标（`wfd/wxd/reg_idxw`）、`mask`（哪些 lane 要写回）、`isvec/unsigned`、每 lane 的 `wordOffset1H`（用于返回时做字节提取）、`isWrite`。它在指令进入时存进 MSHR，等数据收齐后再原样取出，驱动写回——所以 LSU 不需要再单独缓存控制信号。

**`DCacheCoreReq_np`** —— 一次 DCache 请求：

[ventus/src/pipeline/LSU.scala:37-48](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/LSU.scala#L37-L48) —— `instrId`（回程对号用）、`tag/setIdx`（定位 cacheline）、每 lane 的 `DCachePerLaneAddr`（`activeMask` + `blockOffset` + `wordOffset1H`，见 [LSU.scala:31-35](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/LSU.scala#L31-L35)）、`data`（写数据）、TileLink 风格的 `opcode/param`。`asid` 仅当 `MMU_ENABLED` 时存在。

**`ShareMemCoreReq_np`** —— 一次 SharedMemory 请求：

[ventus/src/pipeline/LSU.scala:66-74](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/LSU.scala#L66-L74) —— 与 dcache 版类似但更简：不带 tag、只有 `setIdx`、`isWrite`、每 lane 地址与数据。

**`DCacheCoreRsp_np`** —— 响应：

[ventus/src/pipeline/LSU.scala:50-59](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/LSU.scala#L50-L59) —— `instrId` + `data`（每 lane 一个 word）+ `activeMask`（这一批返回了哪些 lane）。DCache 与 SharedMemory 的响应**共用同一个** `DCacheCoreRsp_np`，所以 `rspArbiter` 才能把两路直接合并。

#### 4.3.4 代码实践

**实践目标**：理清 `instrId` 如何把“请求—响应—MSHR 表项”三者串起来。

**操作步骤**：

1. 在 [LSU.scala:386](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/LSU.scala#L386) 处看到 `s_save` 状态下 `reg_entryID := io.idx_entry`（保存 MSHR 分配的表项号）。
2. 在 [LSU.scala:213](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/LSU.scala#L213) 与 [LSU.scala:227](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/LSU.scala#L227) 看到 `to_shared/to_dcache.bits.instrId := reg_entryID`（写进请求）。
3. 在 [MSHR.scala:71](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/MSHR.scala#L71) 看到 `data.write(io.from_dcache.bits.instrId, ...)`（响应按 instrId 回填）。

**需要观察的现象 / 预期结果**：`instrId` 是一个贯穿请求与响应的“快递单号”。一条向量指令若被 coalesce 成多次 cache 请求，这多次请求的 `instrId` **相同**（都指向同一个 MSHR 表项），因此多次返回能被正确累积到同一条指令。

#### 4.3.5 小练习与答案

**练习**：为什么 `MshrTag` 里要保存每 lane 的 `wordOffset1H`，而不是在写回时重新算？

**答**：因为字节提取（半字/字节 load 需要符号扩展或零扩展）依赖**当初访存指令的 `mem_whb` 与地址低位**，而这些信息在数据从 cache 返回时已经不在响应里了。`MshrTag` 把每 lane 的 `wordOffset1H` 冻结下来，供 [MSHR.scala:100-104](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/MSHR.scala#L100-L104) 的 `ByteExtract` 在收齐后统一使用。

---

### 4.4 MSHRv2：在途请求记录与结果收集

#### 4.4.1 概念说明

`MSHRv2`（实例名 `Coalscer`）是 LSU 的“收发台账”。它解决两个矛盾：

1. **一条指令可能分多批返回**：coalesce 让一条向量访存拆成多次 cache 请求，这些请求可能乱序、分批返回，必须有人把它们重新归并到同一条指令。
2. **cache 侧不懂 warp/lane**：cache 只会回“某个 instrId 的某几个 lane 数据”，需要 LSU 侧的表来还原成完整的写回包。

#### 4.4.2 核心流程

`MSHRv2` 用三张表（`data`/`tag`/`currentMask`）+ 一个 `used` 位图，管理 `lsu_nMshrEntry = num_warp` 个表项：

```
新指令到达(from_addr):
    valid_entry = 第一个空闲表项 (~used 的 PriorityEncoder)
    tag[valid_entry]     := MshrTag            # 存身份卡
    currentMask[valid_entry] := 该指令的 mask   # 初始化“待收 lane 集合”
    used[valid_entry]    := 1
    返回 idx_entry = valid_entry 给 AddrCalculate

cache 响应到达(from_dcache, 带 instrId e):
    data[e]      := 写回这批 lane 的数据（按 activeMask 部分写）
    currentMask[e] := currentMask[e] AND NOT(activeMask)   # 清掉已到的 lane

某表项 currentMask==0 且 used:
    complete := 1                                # 该指令全部 lane 收齐
    输出: 按 MshrTag.wordOffset1H 对每 lane做ByteExtract → to_pipe
    释放: used[output_entry] := 0
```

核心不变量：`currentMask(e)` 记录表项 e **还差哪些 lane**；归零即代表该指令所有数据到齐。注意 `currentMask` 用 0 表示“完成”，这与活动 mask 用 1 表示“有效”恰好相反——代码里用 `inv_activeMask = ~activeMask` 来做对接（[MSHR.scala:35](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/MSHR.scala#L35)）。

#### 4.4.3 源码精读

存储与派生信号：

[ventus/src/pipeline/MSHR.scala:31-39](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/MSHR.scala#L31-L39) —— `complete` = “currentMask 全 0 且 used”；`output_entry` = 第一个 complete 的表项；`valid_entry` = 第一个空闲表项。

握手与表项分配：

[ventus/src/pipeline/MSHR.scala:45-47](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/MSHR.scala#L45-L47) —— `from_addr.ready` 要求“有空闲表项”（`!(used.andR)`）；`idx_entry` 在 fire 当拍给出新分配的表项号。

登记/回填/释放三段（主 `switch`）：

[ventus/src/pipeline/MSHR.scala:67-96](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/MSHR.scala#L67-L96) —— `s_idle` 里若响应与新增请求同拍到达，则先把新增请求存到 `reg_req`、下一拍在 `s_add` 处理（[MSHR.scala:72](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/MSHR.scala#L72)），避免一拍内既写 `used` 又冲突。

输出时的字节提取：

[ventus/src/pipeline/MSHR.scala:97-108](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/MSHR.scala#L97-L108) —— 读出 `output_tag` 与 `raw_data`，对每个 lane 用 `ByteExtract(unsigned, raw_data(x), wordOffset1H(x))`（[LSU.scala:95-111](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/LSU.scala#L95-L111)）做符号/零扩展，最终组装成 `MSHROutput` 送 `to_pipe`。

`LSU2WB` 再把 `MSHROutput` 按 `wxd/wfd` 分流到标量/向量写回端口：

[ventus/src/pipeline/LSU.scala:490-529](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/LSU.scala#L490-L529) —— 写指令（`isWrite`、无写回目标）走 `otherwise` 分支，仅消费响应不产生写回。

#### 4.4.4 代码实践

**实践目标**：理解“一条指令多次返回如何被累积”。

**操作步骤**：

1. 假设一条向量 load 被 coalesce 成 2 次 dcache 请求（`instrId=3`），第一次返回 lane 0~15，第二次返回 lane 16~31。
2. 对照 [MSHR.scala:69-71](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/MSHR.scala#L69-L71)，推演 `currentMask[3]` 的变化：初值 `0x0000_FFFF_FFFF`（32 位全 1）→ 第一次后变 `0xFFFF_0000` → 第二次后变 `0`。
3. 当 `currentMask[3]==0` 时 `complete` 拉高，触发 `s_out` 输出。

**需要观察的现象 / 预期结果**：尽管两次返回可能间隔很多拍，甚至中间夹着别的 warp 的指令返回，表项 3 始终保留这条指令的 `tag` 与已收数据，直到收齐才释放。这正是“非阻塞”的本质——LSU 不会因为某条指令未完成而卡住后续指令的发射（后续指令只要还有空闲 MSHR 表项就能继续）。

**待本地验证**：可在 `from_dcache.fire` 处打印 `instrId` 与 `activeMask`，观察同一 `instrId` 的多次返回。

#### 4.4.5 小练习与答案

**练习**：`lsu_nMshrEntry = num_warp`（默认 8）。这意味着同一时刻整个 SM 最多有多少条在途访存指令？若某 warp 想发第 9 条会怎样？

**答**：最多 8 条在途（与 warp 数相同，粗略地每个 warp 平均 1 条）。当 `used.andR`（8 个表项全满）时，`from_addr.ready=0`（[MSHR.scala:46](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/MSHR.scala#L46)），AddrCalculate 卡在 `s_save` 无法前进，进而反压上游 Issue。这是 LSU 的结构性反压。

---

### 4.5 fence 同步与 ShiftBoard 反压

#### 4.5.1 概念说明

GPU 程序里常有“确保之前的所有 store 都落地，再继续”的需求（类似 RISC-V 的 `fence`）。Ventus 的做法是：`fence` 指令进记分板后，把**同 warp 后续的所有访存指令**挡住，直到该 warp 之前发出的所有访存请求都返回——由 `ShiftBoard` 计数、`fence_end` 信号解锁。

#### 4.5.2 核心流程

```
每 warp 一个 ShiftBoard（深度 lsu_num_entry_each_warp=4 的移位寄存器）:
    left  = lsu_req.fire  且 wid 匹配   # +1（该 warp 又发一条访存）
    right = lsu_rsp.fire  且 wid 匹配   # -1（该 warp 又完成一条）
    empty = 移位寄存器全 0              # 该 warp 无在途访存
    full  = 最高位为 1                  # 该 warp 在途数达上限，反压新请求

fence_end(i) = shiftBoard(i).empty      # 拼成 num_warp 位的 Output

记分板(scoreboard):
    fenceReg.set(  if_fire & ctrl.fence )        # fence 指令发射时置锁
    fenceReg.clear( fence_end )                  # 该 warp 在途全清时解锁
    readf = ibuffer_if_ctrl.mem & fenceReg       # 锁住后续访存指令
```

两层反压：

1. **fence 语义反压**：`fence` 之后的 mem 指令被记分板挡住，直到 `fence_end`。
2. **结构性反压**：某 warp 在途访存达到 `lsu_num_entry_each_warp=4` 时 `full=1`，`lsu_req.ready` 被拉低（[LSU.scala:578-579](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/LSU.scala#L578-L579)），阻止该 warp 再发新访存。

#### 4.5.3 源码精读

`ShiftBoard` 是一个移位寄存器实现的计数器（用“1 的个数”代表在途数量）：

[ventus/src/pipeline/LSU.scala:587-605](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/LSU.scala#L587-L605) —— `left_move`（入）把 1 从低位推入、`right_move`（出）把 1 从高位弹出；`full` 看最高位，`empty` 看最低位。

`LSUexe` 里例化 `num_warp` 个 ShiftBoard 并生成 `fence_end`：

[ventus/src/pipeline/LSU.scala:572-579](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/LSU.scala#L572-L579) —— `fence_end := VecInit(shiftBoard.map(_.empty)).asUInt`；同时用 `full` 反压 `lsu_req`。

记分板侧的对接：

[ventus/src/pipeline/scoreboard.scala:107](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/scoreboard.scala#L107) / [scoreboard.scala:119-120](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/scoreboard.scala#L119-L120) / [scoreboard.scala:133](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/scoreboard.scala#L133) —— `fenceReg` 是 1 位的 ScoreboardUtil，set/clear 如上；`readf` 参与 `delay`（见 u4-3），从而把 fence 之后的 mem 指令按在 ibuffer 队头。

`pipe.scala` 里把每 warp 的 `fence_end` 喂给对应记分板：

[ventus/src/pipeline/pipe.scala:241](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L241) —— `scoreb(i).fence_end:=lsu.io.fence_end(i)`。

> 补充：`flush_dcache`（kernel 结束时 invalidate L1D，见 [LSU.scala:262-264](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/LSU.scala#L262-L264) 与 [pipe.scala:168-171](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/LSU.scala#L168-L171)）复用了 AddrCalculate 的状态机走一拍 `opcode=3`（Prefetch/Invalidate）请求，与本讲的 fence 是两套不同的“清理”机制，注意区分。

#### 4.5.4 代码实践

**实践目标**：看清 fence 如何把“等访存完成”做成硬件信号。

**操作步骤**：

1. 在 [scoreboard.scala:119-120](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/scoreboard.scala#L119-L120) 确认 `fenceReg` 的 set（fence 发射）与 clear（`fence_end`）条件。
2. 在 [LSU.scala:572-577](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/LSU.scala#L572-L577) 确认 `fence_end` 来自每 warp ShiftBoard 的 `empty`。
3. 串联：fence 发射 → fenceReg 置 1 → 同 warp 后续 mem 指令的 `readf=1` → `delay=1` → ibuffer2issue 停止发射该 warp 的 mem 指令；待该 warp 所有在途访存返回（ShiftBoard 清空）→ `fence_end=1` → fenceReg 清 0 → 解锁。

**需要观察的现象 / 预期结果**：fence 不会立刻阻塞（fence 自己不是 mem 指令），它阻塞的是**它之后的访存指令**；且只阻塞同 warp，不影响别的 warp 继续跑——这是 SIMT 延迟隐藏的体现。

**待本地验证**：写一个含 `fence` 的 kernel（fence 前有若干 store），在仿真里观察 `lsu.io.fence_end(wid)` 从 0 变 1 的时刻是否对应该 warp 最后一条 store 的 `lsu_rsp.fire`。

#### 4.5.5 小练习与答案

**练习**：`ShiftBoard` 的深度是 `lsu_num_entry_each_warp=4`，而 MSHR 的表项总数是 `lsu_nMshrEntry=num_warp=8`。这两个限制各自约束什么？

**答**：MSHR 表项（8）约束的是**整个 SM** 同时在途的访存指令总数（结构性反压，所有 warp 共享）；ShiftBoard 深度（4）约束的是**单个 warp** 同时在途的访存指令数（防止某个 warp 独占 MSHR、饿死其他 warp）。两者是“全局上限 + 每路上限”的双重反压。

---

## 5. 综合实践

**任务**：以一条 `VLSE32_V`（strided 向量 load，`mop` 非 0 非 3，步长来自 `in2`）为例，画出它从进入 LSU 到写回寄存器堆的完整时序图。

要求覆盖：

1. **地址计算**：写出至少 4 个 lane 的地址公式（`in1 + lane*in2[lane]`），说明 stride 较大时为何 coalesce 效率差、可能拆成多次 cache 请求。
2. **路由**：若把基址改到 `LDS_BASE` 区间，对照 [LSU.scala:162-167](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/LSU.scala#L162-L167) 说明它走 `s_shared` 而非 `s_dcache`。
3. **MSHR 闭环**：标注 `instrId` 在 `to_mshr`(申请) → `to_dcache`(请求) → `from_dcache`(响应) → `to_pipe`(收齐输出) 四个环节的流转，以及 `currentMask` 如何从全 1 逐步清零。
4. **反压**：若该 warp 紧跟一条 `fence`，标出 `fenceReg` 置位/清除的时刻与 `fence_end` 的来源。

**交付**：一张时序图（手绘或文字描述均可）+ 一段说明，解释 coalesce 拆分次数与 stride、cacheline 对齐的关系。若条件允许，用 `sim-verilator` 跑一个 strided load 的测试用例，在 LSU 关键信号上打印验证你的推演（**待本地验证**）。

## 6. 本讲小结

- **LSU 是 SIMT 与 cache 之间的翻译层**：它把“一条向量访存指令”翻译成 cache 能理解的 cacheline 请求，对 cache 屏蔽了 warp/lane/mask 概念。
- **AddrCalculate 用 FSM 完成“算地址 → 判路由 → 合并 → 更新 mask”四步循环**：同 cacheline 的 lane 合并成一次请求，不同 cacheline 分多拍发出。
- **路由靠地址范围**：全部活动 lane 落在 `[LDS_BASE, +128KiB)` 走 SharedMemory，否则走 DCache；被 mask 关闭的 lane 不影响路由判定。
- **三个 Bundle 各司其职**：`MshrTag` 是指令身份卡（驱动写回），`DCacheCoreReq_np`/`ShareMemCoreReq_np` 是 cacheline 请求；贯穿三者的 `instrId` 是请求—响应—MSHR 表项的“快递单号”。
- **MSHRv2 用 `currentMask` 记录每条指令“还差哪些 lane”**：分批返回按 `instrId` 累积，归零即收齐，从而支持非阻塞的乱序返回。
- **fence + ShiftBoard 实现 per-warp 的访存完成等待**：`fence_end = ShiftBoard.empty` 解锁记分板；另有 MSHR 表项上限与每 warp 在途上限两重结构性反压。

## 7. 下一步学习建议

本讲只讲到“LSU 把请求交给 DCache/SharedMemory 的接口”。接下来：

- **u6-l2（L1 数据缓存）**：继续往下读 `DataCache`，看 DCache 如何处理 LSU 发来的 `DCacheCoreReq_np`（写穿/写不分配、WSHR），理解 LSU 里的 MSHR 与 DCache 内部的 MSHR 是两层不同的表。
- **u6-l4（共享内存 SharedMemory）**：读 `SharedMemory` 的 bank 化组织与 `BankConflictArbiter`，理解 `ShareMemCoreReq_np` 落到 SRAM 后如何处理 bank 冲突。
- **u6-l3（Tag 访问与 MSHR 机制）**：系统对比 L1 的 `L1MSHR`（primary/secondary miss）与本讲的 `MSHRv2`，厘清“LSU 的 MSHR”与“cache 的 MSHR”分工。
- 若对私有内存（PDS）寻址感兴趣，可回头细读 [LSU.scala:148-150](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/LSU.scala#L148-L150) 中 `is_vls12` 分支与 `csr_pds/csr_numw/csr_tid` 的协作，并结合 u2-l1 的 CSR 约定。
