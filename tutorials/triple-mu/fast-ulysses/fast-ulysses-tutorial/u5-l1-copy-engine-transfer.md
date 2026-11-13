# 拷贝引擎传输：launch_a2a_ce 精读

## 1. 本讲目标

前几讲我们把一次 all-to-all 的「寻址」讲完了：`build_plan` 把 4D 交换描述成一组无指针的 `CopyOp`（源/目的偏移 + 步长 + 行数），并把它们装进 `A2APlan`。本讲回答下一个问题：**这些 `CopyOp` 在 GPU 上到底由谁、在哪条流、按什么顺序执行？**

学完本讲你应该能：

1. 把 `CopyOp` 的每个字段逐一映射到 `cudaMemcpy2DAsync` / `cudaMemcpy3DAsync` 的参数，并解释 `depth` 字段何时把批维折叠成一次 3D 拷贝、何时退化为多次 2D 拷贝。
2. 解释 peer 枚举的两段式设计：`rank ^ k` 的 XOR 顺序覆盖哪些 peer，非 2 幂 world_size 时补漏扫描（sweep）又补上哪些。
3. 说明本 rank 自份额为什么走调用者流而不是 xfer 流——核心是「零 SM 是远端拷贝的性质，同设备拷贝会与计算竞争内存系统」。
4. 画出两条流之间用 `ready` / `done` 两个事件汇合的时序图，并复述「事件每次新建、不能提升为组级共享」的循环等待论证。

## 2. 前置知识

**拷贝引擎（CE / DMA engine）。** GPU 上除了执行通用计算的流式多处理器（SM），还有独立的拷贝引擎（copy engine，也称 DMA engine），专门执行数据搬运。普通 kernel 必须等 SM 上有空闲的线程块槽位才能启动，而 `cudaMemcpy*Async` 一类的拷贝由 CE 执行：它不占用任何 SM。这就是本库「零 SM 传输」的物理来源——但注意，**只有跨 NVLink 链路的远端拷贝由 CE 搬运**；同一设备内的拷贝走内存系统，会和计算竞争带宽（本讲 4.3 展开，u5-l2 讲怎么测量）。

**CUDA 流与事件。** 流（stream）是 GPU 任务的先进先出队列：同一条流上的操作按序执行，不同流之间没有顺序。要让流 B 等待流 A 的某个进度点，用事件（event）：`cudaEventRecord(e, streamA)` 在 streamA 的队列里打一个标记，`cudaStreamWaitEvent(streamB, e, 0)` 让 streamB 排队等待这个标记完成。两次调用配合，就把「跨流依赖」表达成了纯设备侧的顺序，不需要主机同步。

**pitched 拷贝。** `cudaMemcpy2DAsync(dst, dpitch, src, spitch, width, height, ...)` 搬运 `height` 行、每行 `width` 字节的二维区块：第 `i` 行源地址是 `src + i*spitch`、目的地址是 `dst + i*dpitch`。当 `spitch > width` 时拷贝的是「带洞」的跨步行。`cudaMemcpy3DAsync` 再加一个 slice 维（可以理解为批维）。u3-l1 已经把 `CopyOp` 的十个字段和这些参数一一对应过，本讲从「发射端」再看一遍。

**承接概念。** 本讲直接使用 u3 建立的词汇：`CopyOp`（带步长拷贝的纯主机描述）、`build_plan`（寻址层）、`window_numel`（全组最大接收量）；以及 u4-l1 的 `Window{tensor, peer_ptrs, flag_ptrs}`——本讲的拷贝目标地址就来自 `peer_ptrs`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/transfer.cu](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/transfer.cu) | CE 传输的全部实现：`issue_copy`（一条 CopyOp → 一次 CUDA 拷贝调用）与 `launch_a2a_ce`（流编排、peer 枚举、事件汇合），以及仅测试用的 `set_ce_fault` 故障注入 |
| [include/fast_ulysses/transfer.hpp](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/transfer.hpp) | 设备侧两个组件（传输 + 屏障）的接口与文档注释，讲清了零 SM 性质的准确边界 |
| [include/fast_ulysses/common.hpp](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/common.hpp) | 全部翻译单元共用的错误检查宏 `ULYSSES_CUDA_CHECK` 和事件 RAII 类 `Event` |
| [src/a2a_plan.cc](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc) | `push_batched`：决定批维是否折叠进一条 3D 拷贝的地方（`issue_copy` 的上游） |
| [src/bindings.cc](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc) | 调用方：`transfer_on_stream` 的「屏障 → 传输 → 屏障」三明治，以及 sync/async 两条路径各传哪条流 |
| [src/group.cc](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc) | `xfer_stream()`：组级专用传输流的惰性创建与销毁 |

一个贯穿全讲的分层事实：`transfer.cu` **不算任何偏移**——寻址全部来自 `build_plan`；它也**不知道 peer 地址从哪来**——`peer_ptrs` 由调用方传入。这让传输层可以独立于 torch 对称内存和任何通信库被阅读（见 [include/fast_ulysses/transfer.hpp:3-5](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/transfer.hpp#L3-L5)）。

## 4. 核心概念与源码讲解

### 4.1 issue_copy：一条 CopyOp 变成一次 cudaMemcpy2D/3DAsync

#### 4.1.1 概念说明

u3-l1 讲过，`build_plan` 输出的 `CopyOp` 是「无指针的步长拷贝」：偏移是相对基址的字节数，这样同一个结构既能描述 `cudaMemcpy*Async`，也能在无 GPU 的测试里用 numpy 重放。`issue_copy` 就是把这个描述落到 CUDA 运行时调用的那一步：**一条 `CopyOp` 恰好对应一次 CUDA 拷贝调用**，中间没有 kernel、没有临时缓冲。

关键分岔在 `depth` 字段：`depth == 1` 是普通 2D 跨步拷贝；`depth > 1` 表示批维（batch）被折叠了进来，用 3D 拷贝一次搬完。折叠与否不是 `issue_copy` 决定的，而是上游 `push_batched` 在建 plan 时决定的——`issue_copy` 只负责按 `depth` 选分支。

还有一个值得注意的细节：拷贝类型传的是 `cudaMemcpyDefault`，让运行时根据指针所属自行推断（peer 窗口经对称内存映射后对本 rank 可直接寻址，属于「对等访问」式搬运）。

#### 4.1.2 核心流程

```
issue_copy(dst, src, op, stream):
    if op.depth <= 1:                      # 单个批元素
        cudaMemcpy2DAsync(dst, op.dst_pitch,      # 目的基址 + 行步长
                          src, op.src_pitch,      # 源基址 + 行步长
                          op.width,               # 每行字节数
                          op.rows,                # 行数
                          cudaMemcpyDefault, stream)
        return
    # depth > 1：批维折叠成第三维
    srcPtr = pitched_ptr(基址=src, pitch=op.src_pitch,
                         xsize=op.width, ysize=op.src_slice / op.src_pitch)
    dstPtr = pitched_ptr(基址=dst, pitch=op.dst_pitch,
                         xsize=op.width, ysize=op.dst_slice / op.dst_pitch)
    extent = (width, rows, depth)           # x=行宽, y=行数, z=批元素个数
    cudaMemcpy3DAsync(parms, stream)
```

3D 分支有一个隐藏约束：`cudaMemcpy3DParms` 结构体**没有 slice 步长字段**——CUDA 会在内部把 slice 步长推导为 `pitch * ysize`。所以 `issue_copy` 传入的 `ysize` 不是「逻辑行数 `op.rows`」，而是 `op.src_slice / op.src_pitch`，即一个批切片占多少「pitch 单位」。推导出的 slice 步长 `pitch * ysize` 恰好等于存进 `CopyOp` 的真实批间距 `src_slice`。这个约束正是上游 `push_batched` 的「可表达性」判据的来源（见 4.1.3）。

#### 4.1.3 源码精读

`issue_copy` 全文只有 20 余行，两个分支：

[src/transfer.cu:26-52](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/transfer.cu#L26-L52) —— 一条 CopyOp → 一次 CUDA 调用。`depth <= 1` 走 `cudaMemcpy2DAsync`，字段与参数一一对应（`dst_pitch`/`src_pitch`/`width`/`rows`）；否则构造 `cudaMemcpy3DParms`，两个 `make_cudaPitchedPtr` 的第四个参数（ysize）由 `op.src_slice / op.src_pitch` 与 `op.dst_slice / op.dst_pitch` **推导得出而非直接传入**，注释明确说明原因是 `cudaMemcpy3DParms` 不收 slice 步长（见 [src/transfer.cu:24-25](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/transfer.cu#L24-L25) 的注释）。

折叠的决策在上游 `push_batched`：

[src/a2a_plan.cc:70-89](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L70-L89) —— `push_batched` 决定「`depth` 个批元素」发射为一条 3D 拷贝还是 `depth` 条 2D 拷贝。两个判据：

- `worth_fusing = depth > 1 && op.rows > 1`：**只融合多行拷贝**。单行拷贝本身就是平坦 memcpy，折进 3D 反而把它挪上跨步路径；而多行的 pitched 拷贝本来就在跨步路径上，融合省掉 \(depth - 1\) 次调用开销。
- `expressible`：3D 参数要能表达批间距——要求 `src_stride % op.src_pitch == 0`、`dst_stride % op.dst_pitch == 0`（间距能折算成整行），且商 `src_stride / op.src_pitch >= op.rows`、`dst_stride / op.dst_pitch >= op.rows`（一个批切片的行数装得下 `rows` 行，相邻切片不重叠——这正对应 4.1.2 说的 `pitch * ysize` 推导）。

满足则把 `depth`/`src_slice`/`dst_slice` 写进 op 一次推入（[src/a2a_plan.cc:76-82](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L76-L82)）；否则退化为循环 `depth` 次、每次偏移 `i * stride` 的 2D 拷贝（[src/a2a_plan.cc:83-88](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L83-L88)）。

以 mode 0（scatter head）为例验证「总是可表达」：`op.rows = s_me`、`src_pitch = head_total * d_bytes`，批间距 `src_stride = s_me * head_total * d_bytes`，于是 \(src_stride / srcPitch = s\_me = rows\)，恰好取等；目的侧 \(dstStride / dstPitch = seq\_total \ge s\_me\)。见 [src/a2a_plan.cc:184-192](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L184-L192)（mode 0 的 CopyOp 构造与 `push_batched` 调用）和 [src/a2a_plan.cc:199-212](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L199-L212)（mode 1 镜像）。结论：**只要 `b > 1` 且本 rank 序列分片长 `s_me > 1`（mode 1 是 `s_p > 1`），批维一定折叠成一条 3D 拷贝**；退化情形只有两类——`b == 1`（没有批维可折）或行数 `rows == 1`（`worth_fusing` 不成立，退化为 `b` 条 2D）。

#### 4.1.4 代码实践

1. **实践目标**：用无 GPU 的 `a2a_plan_debug` 算子统计 `b=1` 与 `b=4`、同一 `(s, n, d)` 下 plan 的 op 数量与 `depth` 字段，验证 4.1.3 的折叠规则。
2. **操作步骤**：新建脚本（示例代码，仅需 import fast_ulysses 注册算子，不需要 GPU 和进程组）：

   ```python
   # practice_ops_depth.py —— 示例代码
   import torch
   import fast_ulysses  # noqa: F401  仅为了注册 torch.ops.fast_ulysses

   def plan_for(b, ws=4, s_local=2, n_local=10, d=64, mode=0, elem=2):
       # 偶均分特例：每 rank 序列分片 s_local、头数 n_local
       seq_splits, head_splits = [s_local] * ws, [n_local] * ws
       return torch.ops.fast_ulysses.a2a_plan_debug(
           b, d, 0, ws, seq_splits, head_splits, mode, elem)

   for b, s_local in [(1, 2), (4, 2), (4, 1)]:
       shape, window_numel, ops = plan_for(b=b, s_local=s_local)
       depth_col = ops[:, 7]          # 列序见下
       print(f"b={b} s_local={s_local}: op数={ops.shape[0]}, "
             f"depth集合={sorted(set(depth_col.tolist()))}, "
             f"peer覆盖={sorted(set(ops[:, 0].tolist()))}")
   ```

   列序来自 [src/plan_bindings.cc:40-49](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/plan_bindings.cc#L40-L49)：第 0 列 peer，第 6 列 rows，**第 7 列 depth**，第 8/9 列 src_slice/dst_slice。
3. **需要观察的现象**：三组输出中 op 数与 depth 集合的组合。
4. **预期结果**（由 `push_batched` 规则推出；运行输出「待本地验证」）：
   - `b=1, s_local=2`：每个 peer（含自己）1 条 op，共 4 条，depth 全为 1 → 全部走 2D 分支；
   - `b=4, s_local=2`：仍 4 条 op（每 peer 一条），depth 全为 4 → 走 3D 分支；**op 数量不变，变的是每条的深度**；
   - `b=4, s_local=1`：rows=1 融合不值得 → 每 peer 4 条、共 16 条，depth 全为 1 → 退化为多次 2D。
5. 写一段注释回答：`push_batched` 何时把 batch 折叠成一次 3D 拷贝（多行 + 间距整除步长且切片不重叠），何时退化为多次 2D（单行或单批元素）。

#### 4.1.5 小练习与答案

**练习 1**：`issue_copy` 里 3D 分支的 `ysize` 为什么传 `op.src_slice / op.src_pitch` 而不是 `op.rows`？

**答案**：`cudaMemcpy3DParms` 没有 slice 步长字段，CUDA 内部把 slice 步长推导为 `pitch * ysize`。要让推导出的步长等于真实批间距 `src_slice`，就必须令 \(ysize = srcSlice / srcPitch\)。`rows` 是本切片内真正要拷的行数，它出现在 `extent.y` 里；而 `ysize` 表达的是「一个批切片占多少行宽」，两者角色不同。

**练习 2**：如果某个 rank 的序列分片 `s_me == 1` 且 `b == 8`，mode 0 下它向每个 peer 发射几条拷贝、每条 depth 是多少？

**答案**：`depth > 1` 但 `rows == 1`，`worth_fusing` 不成立 → 每 peer 8 条 op，每条 depth=1（走 2D 分支）；若某个 peer 的头数为 0（`row == 0`），则该 peer 完全跳过（[src/a2a_plan.cc:180-183](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L180-L183) 的 `continue` 守卫）。

### 4.2 XOR peer 顺序与非 2 幂补漏扫描

#### 4.2.1 概念说明

`plan.ops` 里混着发给所有目的 rank（含自己）的拷贝。发射时要决定**按什么顺序访问 peer**。本库用两段式：

1. **XOR 枚举**：对 \(k = 1, 2, \dots, ws-1\)，访问 \(peer = rank \oplus k\)。
2. **补漏扫描（sweep）**：当 `ws` 不是 2 的幂时，再扫一遍所有 `p`，补上 XOR 漏掉的 peer。

XOR 顺序的好处是「**无协调地两两配对**」：在第 \(k\) 步，rank \(r\) 与 rank \(r \oplus k\) 通信，全体 rank 在同一步恰好构成一组完美匹配（蝶形结构）——每一步每条链路至多被一对 rank 使用，不需要任何全局协商就把链路负载摊开了。注释原文："Peers are visited in XOR-shift order, which pairs ranks up without coordination"（[src/transfer.cu:84-85](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/transfer.cu#L84-L85)）。

XOR 枚举的覆盖能力取决于 `ws` 是否为 2 的幂。当 \(ws = 2^m\) 时，\(rank \oplus k\) 在 \(k \in [1, ws)\) 上取遍 \([0, ws)\) 中除 `rank` 外的每个值**恰好一次**（异或是双射）。当 `ws` 非 2 的幂时，\(rank \oplus k\) 可能 \(\ge ws\)（越界被跳过），于是有些 peer 一次也取不到，需要 sweep 兜底。

#### 4.2.2 核心流程

```
for k in 1 .. ws-1:                 # 主枚举
    peer = rank XOR k
    if peer < ws:                   # 非 2 幂时可能越界，跳过
        emit(peer, xfer)

if ws 不是 2 的幂:                    # (ws & (ws-1)) != 0
    for p in 0 .. ws-1:             # 补漏扫描
        if p != rank and (p XOR rank) >= ws:
            emit(p, xfer)
```

覆盖集合：

- \(ws = 2^m\)：XOR 段覆盖全部 \(ws - 1\) 个 peer，sweep 整段不执行。
- \(ws\) 非 2 幂：XOR 段覆盖 \(\{rank \oplus k : 1 \le k < ws,\ rank \oplus k < ws\}\)，sweep 段补上 \(\{p : p \ne rank,\ p \oplus rank \ge ws\}\)。注意判据用 \(p \oplus rank \ge ws\)：这个 p 无法从任何合法的 k 枚举到（因为若 \(p = rank \oplus k\) 且 \(k < ws\)，XOR 段就会覆盖它），所以 sweep 的条件恰好是「XOR 段取不到的 p」。

举例 \(ws = 6, rank = 4\)（二进制 100）：

- XOR 段：\(k=1..5\) → \(4\oplus1{=}5\)、\(4\oplus2{=}6\)（越界跳过）、\(4\oplus3{=}7\)（跳过）、\(4\oplus4{=}0\)、\(4\oplus5{=}1\) → 覆盖 \(\{5, 0, 1\}\)；
- sweep 段：\(p=2\)（\(2\oplus4{=}6\ge6\) ✓）、\(p=3\)（\(3\oplus4{=}7\ge6\) ✓）→ 补上 \(\{2, 3\}\)；
- 合计 \(\{0,1,2,3,5\}\) = 全部 peer，无重复无遗漏。

（u3-l3 已指出：非 2 幂只影响这里的**传输层枚举**；plan 层对 `ws` 是否 2 幂无感。）

#### 4.2.3 源码精读

[src/transfer.cu:102-107](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/transfer.cu#L102-L107) —— XOR 主枚举：`peer = rank ^ k`，`peer < ws` 才发射，全部落在 **xfer 流**上。

[src/transfer.cu:108-115](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/transfer.cu#L108-L115) —— 补漏扫描：条件 `(ws & (ws - 1)) != 0` 是经典的「非 2 的幂」位判据；sweep 里只挑 `p != rank && (p ^ rank) >= ws` 的 p，注释写明「XOR 只在 ws 为 2 的幂时才枚举到每个 peer，扫描补上它漏掉的」。

[src/transfer.cu:89-95](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/transfer.cu#L89-L95) —— `emit` lambda：遍历 `plan.ops`，挑出 `op.peer == p` 的条目，把目的基址定为 `peer_ptrs[p] + op.dst_offset`、源基址定为 `src_bytes + op.src_offset`，交给 `issue_copy` 发到指定流。偏移在此处才与真实指针相加——寻址与传输的分工边界就在这一行。

顺带一提 [src/transfer.cu:81-83](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/transfer.cu#L81-L83) 的注释：远端拷贝**只用一条流**，因为它们都从同一个出口（egress，即本卡的对互联出口）离开，开多条流只会让它们互相竞争、徒增调度开销。XOR 顺序是「访问顺序」而不是「并行流」。

#### 4.2.4 代码实践

1. **实践目标**：手工模拟两段枚举，确认覆盖集合，并理解 sweep 条件的判据方向。
2. **操作步骤**：在纸上（或用一个 5 行 Python 片段，示例代码）对 `(ws, rank)` ∈ {(8, 3), (6, 4), (6, 1), (5, 2)} 分别计算 XOR 段覆盖与 sweep 段补充：

   ```python
   # practice_xor.py —— 示例代码
   for ws, rank in [(8, 3), (6, 4), (6, 1), (5, 2)]:
       xor_hits = {rank ^ k for k in range(1, ws) if (rank ^ k) < ws}
       sweep    = {p for p in range(ws) if p != rank and (p ^ rank) >= ws}
       print(f"ws={ws} rank={rank}: XOR={sorted(xor_hits)} "
             f"sweep={sorted(sweep)} 并={sorted(xor_hits | sweep)}")
   ```
3. **需要观察的现象**：`ws=8` 时 sweep 是否为空；`ws=6, rank=1` 时 XOR 段是否已经覆盖全部 peer（提示：会——非 2 幂下**部分** rank 的 XOR 枚举仍完备，sweep 只服务那些「被漏」的 rank，但每个 rank 都要执行同一段代码）。
4. **预期结果**：四组的并集都等于 `set(range(ws)) - {rank}`；`ws=8` 的 sweep 为空集。运行输出「待本地验证」。
5. 思考并写注释：如果去掉 `peer < ws` 的检查会发生什么？（`peer_ptrs` 只有 `ws` 项，越界读是未定义行为；同时拷贝会发往不存在的窗口地址。）

#### 4.2.5 小练习与答案

**练习 1**：为什么 sweep 的条件是 `(p ^ rank) >= ws` 而不是「p 不在 XOR 段的覆盖里」？两者等价吗？

**答案**：等价，且前者是 O(1) 判据。若 \(p \oplus rank = k < ws\)，则 XOR 段的 第 \(k\) 步就会枚举到 p（\(rank \oplus k = p < ws\)，不会被跳过）；反之 \(p \oplus rank \ge ws\) 时，能产生 p 的 k 值本身越界，XOR 段永远取不到 p。所以「\(p \oplus rank \ge ws\)」精确刻画了 XOR 段的遗漏集。

**练习 2**：ws=8、rank=5 时，写出 XOR 访问顺序的前三个 peer。

**答案**：\(5\oplus1{=}4\)、\(5\oplus2{=}7\)、\(5\oplus3{=}6\)。第 k 步与 \(rank \oplus k\) 配对：k=1 时 (5,4)、(0,1)、(2,3)、(6,7) 两两交换——这就是「无协调配对」的含义。

### 4.3 自份额走调用者流：一条不跨链路的拷贝

#### 4.3.1 概念说明

u3-l2 讲过一个反直觉的设计：`plan.ops` 覆盖所有 `ws` 个目的，**本 rank 自己的份额也走窗口**（对 `peer == rank` 的 op，`peer_ptrs[rank]` 就是自己的窗口基址，`issue_copy` 退化为同设备拷贝）。这样「窗口的稠密前缀就是完整输出」，copy-out 才能是一次平坦拷贝、零拷贝路径才能直接返回窗口视图（见 [include/fast_ulysses/a2a_plan.hpp:62-64](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/a2a_plan.hpp#L62-L64) 的注释）。

但「走窗口」不等于「走 xfer 流」。本讲的关键区分是：**零 SM 是远端拷贝的性质，不是所有 CE 拷贝的性质**。远端拷贝（写入 peer 的窗口）经 NVLink 由 CE 搬运，不占 SM；自份额拷贝（写入自己的窗口）不跨链路，是同设备拷贝，**与计算竞争同一套内存系统**。接口文档把这层边界写得很清楚（见 4.3.3 第一条链接）。

既然自份额与远端拷贝性质不同，就不该挤在同一条流里串行：远端拷贝在 xfer 流上排队时，自份额拷贝可以同时在调用者流上前进——`emit(rank, stream)` 单独一发，就是让它与 xfer 流并行。

#### 4.3.2 核心流程

一次 sync 调用中，两条流的分工：

```
调用者流 stream（sync 调用 = 用户当前流；async 调用 = comm 流）
  ├─ fast_barrier（开路屏障）              ← bindings.cc
  ├─ cudaEventRecord(ready)                ← 输入已就绪的标记
  ├─ emit(rank, stream)：自份额拷贝（同设备，写自己窗口）
  └─ cudaStreamWaitEvent(stream, done)     ← 等远端拷贝汇合
       ↓ 之后才轮到
  └─ fast_barrier（收尾屏障）              ← 证明"写全部落地"

xfer 流（组级专用传输流）
  ├─ cudaStreamWaitEvent(xfer, ready)      ← 先等输入就绪
  ├─ emit(rank^1, xfer) … emit(peer, xfer) ← 全部远端拷贝（走 CE，不占 SM）
  └─ cudaEventRecord(done, xfer)
```

自份额拷贝夹在 `ready` 记录与 `done` 等待之间，与 xfer 流上的远端拷贝**时间上重叠**；收尾屏障被 `done` 的等待挡住，直到远端字节全部落地才会发布。

#### 4.3.3 源码精读

[include/fast_ulysses/transfer.hpp:19-21](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/transfer.hpp#L19-L21) —— 零 SM 性质的准确边界：「零 SM 是**远端拷贝**的性质：peer 拷贝可与并发计算重叠，而同设备拷贝与它竞争同一内存系统。本 rank 的自份额是同设备拷贝，copying 路径上的 copy-out 也是。」（copy_out 的同设备性质在 [src/bindings.cc:213-223](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L213-L223)。）

[src/transfer.cu:116](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/transfer.cu#L116) —— `emit(rank, stream)`：自份额发到**调用者流**而非 xfer 流，一行落实上面的分工。注释（[src/transfer.cu:83-85](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/transfer.cu#L83-L85)）说明：远端拷贝共用一条流（同一出口），自份额不跨链路、可以与它们并行，所以单独发。

[src/bindings.cc:193-211](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L193-L211) —— 调用方 `transfer_on_stream`：开路屏障（L203，写者等读者——单缓冲窗口复用前的保护）→ `launch_a2a_ce`（L205，传入 `group->xfer_stream()` 与调用者流）→ 收尾屏障（L210，证明全组写落地）。自份额拷贝就嵌在这两道屏障之间、调用者流之上。

[src/group.cc:444-451](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L444-L451) —— `xfer_stream()`：组级传输流**惰性创建**，`cudaStreamNonBlocking` 标志让它不与遗留默认流隐式同步；`destroy()` 时先同步再销毁（[src/group.cc:459-464](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L459-L464)）。

顺带分清「调用者流」在两条入口下分别是谁：sync 算子传 `at::cuda::getCurrentCUDAStream()`（[src/bindings.cc:261-269](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L261-L269)）；async（staged）路径里 Python 已把 comm 流设为当前流，C++ 取到的 `stream` 就是 comm 流（[src/bindings.cc:357-358](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L357-L358) 注释）。所以更准确的说法是：**自份额跟随「发起本次调用的流」**，xfer 流则永远独立。

#### 4.3.4 代码实践

1. **实践目标**：通过源码阅读确认自份额拷贝在整条调用链中的位置与流归属，补全 4.3.2 的时序图。
2. **操作步骤**：
   - 从 [src/bindings.cc:235-240](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L235-L240) 的 `run()` 出发，列出 `prepare → transfer_on_stream → copy_out` 的顺序；
   - 在 [src/transfer.cu:89-116](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/transfer.cu#L89-L116) 中找到 `emit` 的三处调用点（XOR 段、sweep 段、自份额），标注每个调用点传的流；
   - 把 4.3.2 的时序图抄录并补上 `copy_out`（copying 路径下它在收尾屏障之后、同一条调用者流上）。
3. **需要观察的现象**：图上自份额拷贝与远端拷贝应当处于不同泳道且时间区间可重叠；`copy_out` 只在 `out_is_window == false` 时出现。
4. **预期结果**：一张三条泳道（调用者流 / xfer 流 / CE 出口）的文本时序图，能指出「哪两个操作并行、哪个事件封住收尾屏障」。
5. 若有 ≥2 GPU 且装好本库：可用 `nsys profile` 抓一次 quickstart 调用，在时间轴上确认自份额的 `Memcpy DtoD` 落在调用者流、远端拷贝落在 xfer 流；无 GPU 则本实践为纯源码阅读，结论以代码为准（无需运行验证）。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `emit(rank, stream)` 改成 `emit(rank, xfer)`，功能还对吗？代价是什么？

**答案**：功能仍然正确——自份额拷贝同样会在 `done` 记录之前完成，窗口前缀依然完整。代价是串行化：自份额这条同设备拷贝排进 xfer 流，排在所有远端拷贝（或其间），调用者流只能干等，损失了自份额与远端拷贝的重叠窗口；而且它并不需要让出链路带宽的调度照顾。

**练习 2**：为什么「零 SM」宣传语要小心表述？copying 路径（不带 out=）端到端是零 SM 吗？

**答案**：零 SM 只描述**远端拷贝**。copying 路径还有两处同设备拷贝：自份额（本模块）与 copy_out（把窗口搬到新分配的输出张量）。它们走内存系统、与计算竞争。端到端零 SM 需要 `out=` / `lend=True` 走零拷贝路径消掉 copy_out（u4-l3、u5-l2 展开）。

### 4.4 事件汇合：ready 与 done，以及为什么每次新建

#### 4.4.1 概念说明

两条流各自排一部分工作，就必须有两个「汇合点」：

- **`ready`**（调用者流 → xfer 流）：xfer 流要读输入张量 `src`，而输入是调用者流上更早的操作（比如投影 GEMM）产出的。没有 `ready`，xfer 流可能在输入写完之前就开始读。
- **`done`**（xfer 流 → 调用者流）：`launch_a2a_ce` 返回后，调用者流上紧接着就是**收尾屏障**（向全组宣布「我的写全部落地」）。远端拷贝发在 xfer 流上，若调用者流不等 `done` 就到达收尾屏障，就会在字节还在途时发布——这正是故障注入 `set_ce_fault` 故意制造的错（见 4.4.3）。

第二个问题是**事件的生命周期**。最直观的「优化」是把 `ready`/`done` 提升为组级成员、每次调用复用——本库明确拒绝，理由是**循环等待**：一个被共享的事件在先前的等待还未完成时被重新记录，可能让某个等待解析到**更晚的那次记录**上，于是这个等待依赖上了「它正在阻塞的那条流」自己排队的后续工作——环成立，挂死。异步路径上同一时刻可有多次调用在飞（u7-l1），「重记录时旧等待未完成」不是假设而是常态，所以事件必须每次新建。

#### 4.4.2 核心流程

```
Event ready = new(cudaEventDisableTiming)
record(ready, stream)                     # ① 调用者流打标记
wait(xfer, ready)                         # ② xfer 流等 ①
  [可选：故障注入 host func 睡眠]           #    仅测试态
  远端拷贝序列（XOR 段 + sweep 段）
  emit(rank, stream)                      # ③ 自份额（与上面并行）
Event done = new(cudaEventDisableTiming)
record(done, xfer)                        # ④ xfer 流打标记
if 未注入故障:
    wait(stream, done)                    # ⑤ 调用者流等 ④
# 作用域结束：done、ready 逆声明序析构（RAII，延迟到设备用完）
```

事件的「等待在调用时刻捕获依赖、销毁可延迟」两个性质让「每调用新建」几乎无成本：`cudaEventDestroy` 对尚有未完成工作的事件只是延迟回收，不会同步；新建事件是从轻量池里取句柄。两个事件都带 `cudaEventDisableTiming`——只做排序，不计时（计时版 `Event` 用默认 flag，见 [src/bindings.cc:636-638](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L636-L638) 的 `_timed` 算子）。

#### 4.4.3 源码精读

[src/transfer.cu:86-87](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/transfer.cu#L86-L87) 与 [src/transfer.cu:97](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/transfer.cu#L97) —— `ready` 事件：新建后记录在调用者流，`cudaStreamWaitEvent(xfer, ready, 0)` 让 xfer 流等输入就绪。

[src/transfer.cu:77-80](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/transfer.cu#L77-L80) —— **循环等待论证的原文**（这是本讲最重要的注释）：「Fresh events every call -- do not hoist them onto the group. A shared event re-recorded while earlier waits are still pending lets one of those waits resolve against a later record, which makes the wait depend on the very stream it is blocking: a circular wait. Creating them here is depth-safe, since a wait captures the dependency at call time and destroy defers.」

把循环具体化：设想共享的 `done`。调用 1 在 xfer 流记录 `done`，调用者流排队等它；在等待兑现前，主机又发起调用 2（异步路径完全可能），`done` 被**重新记录**在 xfer 流更深处。若调用 1 的等待解析到这次更晚的记录，则「调用者流的等待」依赖「调用 2 的远端拷贝」；而调用 2 的远端拷贝要等 xfer 流先等到调用 2 的 `ready`——它记录在调用者流上、排在调用 1 那个等待**之后**。于是：调用者流等 xfer 流、xfer 流等调用者流，环闭合，两条流互等。

[src/transfer.cu:118-124](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/transfer.cu#L118-L124) —— `done` 事件与汇合：记录在 xfer 流，调用者流 `cudaStreamWaitEvent(stream, done, 0)` 收口。注意这个等待被 `g_fault_delay_us == 0` 守卫——故障注入时**故意跳过汇合**（见下），注释写明「跳过这个汇合就是故障本身，其余一切不变」。

[src/transfer.cu:56-66](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/transfer.cu#L56-L66) 与 [src/transfer.cu:99-101](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/transfer.cu#L99-L101) —— 仅测试用的 `set_ce_fault`：武装后在 xfer 流插入一个睡眠 `delay_us` 的 host 函数把载荷压住，并（配合 4.4.3 上一条的守卫）跳过汇合，使收尾屏障在字节在途时发布。它验证的是「CE 完成写对目的地可见早于随后的 release store」这一未文档化假设——负控制在 u9-l3 的 `ce_ordering.py` 精读。正常构建里 `g_fault_delay_us` 恒为 0（[src/transfer.cu:16-17](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/transfer.cu#L16-L17)）。

[include/fast_ulysses/common.hpp:28-57](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/common.hpp#L28-L57) —— `Event` RAII 类：构造即 `cudaEventCreateWithFlags`，析构 `cudaEventDestroy`（不检查错误——析构不得抛异常）。类注释解释了为什么用手写配对的 `cudaEventCreate/Destroy` 会泄漏：两个调用之间夹着 `ULYSSES_CUDA_CHECK`，一旦抛异常每次调用漏一个事件，而抛异常时往往正是进程还要继续跑的时候。[src/transfer.cu:125](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/transfer.cu#L125) 的注释点出 `done`、`ready` 在作用域尾部按声明的逆序析构。

[src/transfer.cu:68-76](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/transfer.cu#L68-L76) —— `launch_a2a_ce` 的签名：`src`（本 rank 输入基址）、`peer_ptrs`（peer 窗口基址表，尺寸即 world size）、`plan`、`xfer`、`rank`、`stream` 六个参数。它不持有任何组状态——事件、发射顺序全是每次调用的局部安排。

#### 4.4.4 代码实践

1. **实践目标**：画出一次 sync 调用两条流的完整时序图（本讲规格要求的第二个产出）。
2. **操作步骤**：以 4.3.2 的骨架为基础，补全下面五个标记的时序位置——① `ready` 记录、② xfer 等待、③ 自份额拷贝、④ `done` 记录、⑤ 调用者流等待——并在图后回答：
   - ③ 与远端拷贝为什么可以重叠？
   - 若删掉 ⑤，收尾屏障会在什么状态下发布？哪个测试文件专门检验这条假设？
3. **需要观察的现象**：图中 ⑤ 必须位于收尾屏障之前、④ 之后；① 必须位于产生输入的任何计算之后。
4. **预期结果**：一张双泳道时序图 + 两段文字答案（②答案见 4.3；⑤的答案：字节在途时发布屏障，`test/distributed/ce_ordering.py`）。
5. 若本机装好 torch 但无 GPU：用下面的纯 host 伪代码推演「共享事件循环等待」（示例代码，不依赖本库）——

   ```text
   共享 done；调用1: record(done, xfer); wait(streamA, done)
             调用2: record(done, xfer)          ← 旧等待未兑现就重记录
   若调用1的等待解析到调用2的记录：
       streamA → 等 xfer(调用2) → 等 ready2(在 streamA 上, 排在旧等待之后)
       ⇒ streamA 等 streamA，环。
   ```

#### 4.4.5 小练习与答案

**练习 1**：`ready` 和 `done` 都带 `cudaEventDisableTiming`，为什么？哪个算子偏要带计时的事件？

**答案**：这两个事件只承担流间排序，不需要时间戳，禁用计时开销更低。`_timed` 测试算子要用 `cudaEventElapsedTime` 分解各阶段耗时，所以它的 5 个 `marks` 用默认 flag 保留计时（[src/bindings.cc:636-646](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L636-L646)）。

**练习 2**：`Event` 的析构函数为什么「不检查错误」？构造函数又为什么必须检查？

**答案**：析构不检查：析构途中抛异常是 C++ 的未定义行为路径（且本类大量在栈回退时析构），`cudaEventDestroy` 对挂起事件的延迟回收语义也保证了安全；构造检查：创建失败意味着后续每个 CUDA 调用都会用坏句柄，越早抛出 `TORCH_CHECK` 越能定位问题（见 [include/fast_ulysses/common.hpp:41-44](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/common.hpp#L41-L44) 注释）。

**练习 3**：xfer 流为什么要用 `cudaStreamNonBlocking` 创建？

**答案**：不加此标志的流会与遗留默认流隐式同步（legacy default stream 语义），每次 xfer 上的拷贝都可能被动地与用户在默认流上的工作互相等待，破坏「远端拷贝与计算重叠」的设计前提（[src/group.cc:446-449](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L446-L449)）。

## 5. 综合实践

把本讲四个模块串成一份「一次调用的发射清单」分析报告（规格指定的综合任务）：

**任务 A（无 GPU 可做，约 20 行脚本）**：运行 4.1.4 的脚本，对 `ws=4, n_local=10, d=64, mode=0, elem=2`、`(b, s_local) ∈ {(1,2), (4,2), (4,1)}` 三组各打印一行统计（op 数、depth 集合、peer 覆盖）。然后：

1. 任选一条 `depth=4` 的 op，手工核对它的 `src_slice / src_pitch` 与 `rows` 的关系，验证 4.1.2 的 ysize 推导；
2. 写出解释：为什么 `b=1 → b=4` 时 op **数量**不变而 `depth` 变 4；为什么 `s_local=1` 时退化为 16 条 2D 拷贝；
3. 对照 [src/a2a_plan.cc:72-75](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L72-L75) 的 `worth_fusing`/`expressible` 逐条判据，把三组结果映射回判据。

**任务 B（源码阅读型）**：完成 4.4.4 的双泳道时序图，在图上额外标注：XOR 枚举产生的 peer 访问顺序（用 `ws=4, rank=2` 具体化）、自份额拷贝的位置、`ready`/`done` 两个汇合点、以及故障注入模式下被跳过的那个等待（[src/transfer.cu:122-124](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/transfer.cu#L122-L124)）。

**任务 C（思考题，写进报告结尾）**：如果把 `ready` 提升为组级共享事件（`done` 保持每调用新建），在 u7-l1 的异步路径（comm 流上连续发起多次调用）下构造一个具体的交错序列，说明是否仍会形成循环等待。

**验收标准**：A 的三行统计与判据映射一致（运行结果「待本地验证」）；B 的图能回答「哪个事件封住收尾屏障」；C 给出明确的交错步骤或论证「不会成环」的理由。

## 6. 本讲小结

- `issue_copy` 把一条 `CopyOp` 变成恰好一次 CUDA 拷贝调用：`depth <= 1` 走 `cudaMemcpy2DAsync`，`depth > 1` 走 `cudaMemcpy3DAsync`，且 3D 的 ysize 由 `slice / pitch` 推导——因为 `cudaMemcpy3DParms` 不收 slice 步长。
- 批维折叠由上游 `push_batched` 决定：多行（`rows > 1`）且批间距整除步长、切片不重叠时折叠成一条 3D，否则退化为多条 2D；`b=1` 或 `rows=1` 是两类退化情形。
- peer 按 `rank ^ k` 的 XOR 顺序枚举（无协调的两两配对）；`ws` 为 2 的幂时恰好遍历全部 peer，非 2 幂时用 `(p ^ rank) >= ws` 的补漏扫描兜底。
- 远端拷贝全部串行在组级 xfer 流上（同一出口，多流徒增竞争），**自份额拷贝走调用者流**——因为零 SM 是远端拷贝的性质，同设备拷贝会与计算竞争内存系统。
- 两条流用 `ready`（保输入就绪）与 `done`（保收尾屏障不在字节在途时发布）汇合；事件每次调用新建，因为共享事件在旧等待未兑现时被重记录会形成「等待依赖它所阻塞的流」的循环等待。
- `set_ce_fault` 是仅测试用的故障注入：压住载荷并跳过汇合，为「CE 写可见性」假设提供负控制（u9-l3 展开）。

## 7. 下一步学习建议

- **u5-l2 零 SM 性质的来源与测法**：本讲只给出了「远端 vs 同设备」的定性区分，下一讲用 `bench_a2a.py` 的 zerosm 双臂 A/B 把它变成可测量的结论。
- **u5-l3 标志位屏障**：本讲两次出现的 `fast_barrier` 在下一讲逐行精读——自旋内核、release/acquire 内存序、设备侧 epoch 与 CUDA graph 重放。
- **u7-l1 异步调用全景**：本讲刻意留下的伏笔——async 路径下「调用者流」实为 comm 流、多次调用同时在飞——在高级单元展开。
- 建议顺带重读 [src/transfer.cu:77-85](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/transfer.cu#L77-L85) 的整段注释：它是全文件设计决策（单流、XOR、自份额分流、事件新建）的浓缩版，读完本讲再读会有完全不同的吸收率。
