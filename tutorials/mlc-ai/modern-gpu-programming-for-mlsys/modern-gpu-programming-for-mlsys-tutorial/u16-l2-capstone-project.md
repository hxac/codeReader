# 综合实战：定制你的内核变体（Capstone）

## 1. 本讲目标

这是整套手册的毕业实践。前面十五个单元分别讲了硬件、布局、TMA、Tensor Core、异步协调、TIRx 编程模型、GEMM 九步优化、Flash Attention 4 与工具链；本讲不再引入新的硬件机制，而是要求你**独立地把它们串起来**：从一个已经能对齐 cuBLAS 的内核（GEMM Step 9）或一个 SOTA 注意力内核（FA4）出发，设计并实现一个属于你自己的变体（variant），完整走过：

1. **设计**：提出变体命题，用资源账（SMEM/TMEM/寄存器）先做可行性闸门，再写出 scope/layout/dispatch 三要素与屏障协议设计文档。
2. **实现**：改动参数或内核代码，一次只改一处交接。
3. **验证**：先数值断言（PASS 才继续），无 GPU 时做完整源码推演。
4. **评测**：按附录基准协议计时、归因；无 GPU 时给出性能预测与风险清单。
5. **（可选）贡献**：把有价值的变体整理成对本书或 tirx-kernels 的贡献。

学完本讲，你应该能独立回答三个问题：这个变体值得做吗（资源账与机制收益）？改哪些地方才不会错（三要素与屏障协议文档）？怎么证明它又对又快（验证与评测方案）？

## 2. 前置知识

本讲是纯综合题，所有机制都来自前面的讲义，这里只做最简回顾，细节请回查对应讲义。

- **三要素**：每个 tile 操作由 scope（哪些线程执行）、layout（数据落在哪个物理位置）、dispatch（走哪条硬件路径）刻画；GEMM 九步的每一步都可以用「三要素中哪几项变了」来记录（u9-l3，u11～u13）。
- **屏障类型由完成者决定**：TMA 引擎完成用带字节计数的 `TMABar`；Tensor Core 完成靠 `tcgen05.commit` 的 `TCGen05Bar`；普通线程到达用 `MBarrier`（u7-l1，u8-l1）。
- **相位规则**：full/empty 双屏障各配一个相位，初始相位「资源起始可用的一端给 1、不可用的一端给 0」，设错会死锁或静默读旧数据（u8-l2）。
- **资源上限**（B200）：每 SM 共享内存约 228 KB；TMEM 每 CTA 512 列（128 lane × 512 列 × 32 bit）；寄存器经 `setmaxnreg` 按角色再分配，总量 65536 个 32-bit 寄存器每 CTA（u2-l2，u7-l3，u14-l2）。
- **基准纪律**：正确性先行、明确计时边界、CUDA events 测流内时间、锁定时钟与统一条件、剖析只在诊断时用（u15-l4、u15-l5）。
- **调试方法**：roles/storage/handoff/lifetime 四行工作表 + 生成 CUDA 检视 + 按症状分类排查（u15-l7）。

两个本讲特有的术语：

- **变体（variant）**：在既有内核上做的一处（或一组联动）可评估的改动，如换 tile 形状、换精度、换调度策略、换掩码规则。变体必须保持数学上等价或误差可论证。
- **立项闸门**：在写任何内核代码之前先用纸面核算否决不可行方案的步骤。本讲会反复用到它。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [chapter_gemm_advanced/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md) | GEMM Step 7/8/9：warp 特化、双 CTA cluster、多消费者。Step 9 内核 `hgemm_v9` 是本讲 A 轨（GEMM 变体）的出发点 |
| [chapter_flash_attention/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md) | FA4：算法结构、warp 角色、屏障表、causal/GQA/调度、编译验证。B 轨（FA4 变体）的出发点 |
| [appendix/benchmarking_gpu_kernels.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/benchmarking_gpu_kernels.md) | 基准与剖析协议：正确性先行、计时边界、CUDA events、条件一致性、吞吐换算 |
| [appendix/debugging_warp_specialized.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/debugging_warp_specialized.md) | 调试工作流与 roles/storage/handoff/lifetime 工作表，是本讲设计文档的直接模板 |
| [README.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/README.md) | 运行环境（Blackwell、apache-tvm、tirx-kernels）与贡献入口 |

---

## 4. 核心概念与源码讲解

### 4.1 变体设计：从「机制菜单」到可立项的变体命题

#### 4.1.1 概念说明

变体设计不是「想一个点子然后写代码」，而是把两个已经验证过的优化主线继续往前推一步。书中九步优化的收尾把这两条主线说得非常清楚：**别让 Tensor Core 等数据；每片搬上芯片的数据多算几次**（见 [chapter_gemm_advanced/index.md:L896-L900](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L896-L900)）。任何一个变体命题都应该能回答：「我的改动作用于哪条主线？作用于主线的哪一环？」

变体的可行改动轴（机制菜单）全部写在内核函数开头的一组常量与少量分支里，这是 TIRx 内核的一个直接好处——**设计的自由度是显式的、可枚举的**。对 `hgemm_v9` 而言，菜单包括：

- **tile 形状轴**：`BLK_M / BLK_N / BLK_K`、`PIPE_DEPTH`、`EPI_N`（epilogue 每次写回的列块宽）。
- **协作与复用轴**：`CTA_GROUP`（1 或 2）、`NUM_CONSUMER`（几个 MMA 消费者共享 B）。
- **调度轴**：tile scheduler 的类型与参数（`l2_group_size`、`num_clusters`），或换成 CLC 动态调度（u8-l3）。
- **精度轴**：`a_type / b_type / d_type / acc_type`，以及 block-scaled 路线（MXFP8/NVFP4 的 SFA/SFB，见 u5-l3、u7-l2）。
- **算法/掩码轴**（FA4 侧）：causal 与否、`rescale_threshold`、调度器选型（Linear vs LPT）。

但菜单不等于随便点。每一轴都受三类硬约束：**资源约束**（SMEM 228 KB、TMEM 512 列、寄存器总量）、**正确性约束**（维度整除、数值表示域、数学等价性）、**机制约束**（如 SWIZZLE_128B 下最内连续维不得超过 128 字节，fp16 恰 64 元素，见 u6-l1）。变体设计的第一步就是把命题放到这三类约束上过筛。

#### 4.1.2 核心流程

一个可立项的变体要依次通过四道关卡：

```text
命题（我想改什么、预期作用于哪条主线）
  ↓
① 资源闸门：SMEM / TMEM / 寄存器 三张账，超限即否决或触发联动改动
  ↓
② 正确性闸门：维度整除？数值范围落在 dtype 表示域内？数学是否等价？
  ↓
③ 机制闸门：改动是否与 swizzle 宽度、MMA 形状、屏障类型兼容？
  ↓
④ 收益假设：预期改善哪个指标（L2 命中？Tensor Core 空闲？）——写成可证伪的一句话
```

资源账公式（针对 `hgemm_v9` 一族内核，字节均按 fp16 的 2 字节计）：

\[ \text{SMEM} \approx \underbrace{P \cdot C \cdot M_b \cdot K_b \cdot s}_{\text{Asmem}} + \underbrace{P \cdot N_b \cdot K_b \cdot s}_{\text{Bsmem}} + \underbrace{C \cdot M_b \cdot E \cdot s}_{\text{Dsmem}} + 1\,\text{KB（控制对象区）} \]

其中 \(P\) 是 `PIPE_DEPTH`，\(C\) 是 `NUM_CONSUMER`，\(s\) 是每元素字节数，\(E\) 是 `EPI_N`。TMEM 账更简单：

\[ \text{TMEM 列} = C \times \text{MMA\_N}, \qquad \text{MMA\_N} = N_b \times \text{CTA\_GROUP} \le 512 \]

对照 B200 的每 SM 228 KB SMEM 上限，书中 Step 7 一节已给出每 stage 32 KB 的估算方法（[chapter_gemm_advanced/index.md:L315-L323](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L315-L323)）；本讲把同样的算术推广到 Step 9 的双消费者形状。

#### 4.1.3 源码精读

**机制菜单的物理位置。** `hgemm_v9` 的全部自由度集中声明在函数头部，这是设计变体时第一个要看的地方：

[chapter_gemm_advanced/index.md:L674-L688](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L674-L688) 定义了 `hgemm_v9(M, N, K)`：四个 dtype（fp16 输入输出、fp32 累加），然后是 `CTA_GROUP=2`、`NUM_CONSUMER=2`、`BLK_M, BLK_N, BLK_K = 128, 128, 64`、`MMA_N = BLK_N * CTA_GROUP = 256`、`PIPE_DEPTH=4`、`EPI_N=64`、`WG_NUMBER=3`。改任何一行都要问：这个常量还出现在哪些地方（布局、屏障深度、调度器、循环长度）？

**基线资源账（贴边运行）。** 把上述常量代入 4.1.2 的公式：

- Asmem = 4 × 2 × 128 × 64 × 2 B = 128 KB
- Bsmem = 4 × 128 × 64 × 2 B = 64 KB
- Dsmem = 2 × 128 × 64 × 2 B = 32 KB
- 合计 ≈ 224 KB（另有约 1 KB 控制对象区），对比 228 KB 上限——**Step 9 已经贴着 SMEM 天花板运行**；TMEM 侧 2 × 256 = 512 列，恰好占满（对应内核里一次性 `alloc(n_cols=512)` 再按消费者切片，见 [chapter_gemm_advanced/index.md:L737-L739](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L737-L739)）。这个「贴边」事实直接决定了哪些变体在账面上就不可行（见下面的实践）。

**调度轴的真实参数与告诫。** FA4 一章明确说调度常量是按本书 B200 配置调的、不是普适参数：`max_ctas=148` 封顶 non-causal 持久 worker 数，`L2_SIZE=50 MiB` 是计算 `L2_SWIZZLE` 时假设的可用缓存预算，「不同 SM 数或缓存配置的 Blackwell 应重新调参」（[chapter_flash_attention/index.md:L842-L844](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L842-L844)）。这意味着「调度参数重调」本身就是一类正当、低风险的变体。

**数值域约束的真实例子（FA4 的阈值）。** FA4 延迟重缩放的阈值 \(\tau=8\) 来自论文的取舍：\(-\delta=8\) 时保留旧参考会让当前块的最大未归一化权重达到 \(2^8=256\)（[chapter_flash_attention/index.md:L74-L76](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L74-L76)），代码里对应 `rescale_threshold` 当前为 8.0（[chapter_flash_attention/index.md:L279-L280](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L279-L280)）。关键在于：**P 是以 fp16 写回 TMEM 的**（「its fp16 weight tile」，[chapter_flash_attention/index.md:L326-L327](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L326-L327)），而 fp16 最大可表示值是 65504，略小于 \(2^{16}=65536\)。于是阈值类变体的硬上限浮现出来：\(\tau\) 若取 16，P 的峰值将触顶溢出为 inf；\(\tau=15\)（\(2^{15}=32768\)）虽在表示域内但几乎没有余量。变体设计必须追踪每个中间量的数值范围与其存储 dtype 的表示域——这就是 ② 正确性闸门的数值版。

**运行前提。** 变体要在 GPU 上跑起来，前提是 Blackwell（sm_100a）+ `apache-tvm==0.26.0` + `cuda-bindings` + CUDA 版 PyTorch，FA4 还需要配套 revision 的 tirx-kernels（[README.md:L51-L84](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/README.md#L51-L84)）。没有这些条件时，本讲的实践走「源码推演」路径（见 4.3.4）。

#### 4.1.4 代码实践：资源账核算脚本

**实践目标**：把资源闸门从「心算」变成「可复跑的脚本」，用一个 Python 函数对任意候选变体自动判定可行性。

**操作步骤**（以下为示例代码，不是项目原有文件）：

```python
# 示例代码：variant_budget.py —— hgemm_v9 一族内核的资源账核算
def gemm_budget(blk_m=128, blk_n=128, blk_k=64, pipe=4, cons=2,
                epi_n=64, cta_group=2, elem_bytes=2,
                smem_limit=228 * 1024, tmem_cols=512):
    a = pipe * cons * blk_m * blk_k * elem_bytes      # Asmem
    b = pipe * blk_n * blk_k * elem_bytes             # Bsmem
    d = cons * blk_m * epi_n * elem_bytes             # Dsmem
    ctrl = 1024                                       # move_base_to(1024) 的控制对象区
    smem = a + b + d + ctrl
    mma_n = blk_n * cta_group
    tmem = cons * mma_n
    return {
        "Asmem": a, "Bsmem": b, "Dsmem": d,
        "SMEM_KB": round(smem / 1024), "SMEM_ok": smem <= smem_limit,
        "TMEM_cols": tmem, "TMEM_ok": tmem <= tmem_cols,
    }

# 1) 复现基线 hgemm_v9：应得 SMEM 225 KB、TMEM 512 列，双双贴边
print(gemm_budget())

# 2) 候选变体逐个过闸门
candidates = {
    "A1 pipe=5":            dict(pipe=5),                       # 加深流水线
    "A2 epi_n=128":         dict(epi_n=128),                    # 加宽 epilogue 列块
    "A3 pipe=3,epi_n=128":  dict(pipe=3, epi_n=128),            # 联动缩减
    "A4 blk_k=128,pipe=2":  dict(blk_k=128, pipe=2),            # 加宽 K 块、缩深度
    "A5 cons=3":            dict(cons=3),                       # 第三个消费者
}
for name, kw in candidates.items():
    print(name, gemm_budget(**kw))
```

**需要观察的现象**：A1 得 273 KB（超限，否决）；A2 得 257 KB（否决）；A3 得 209 KB（账面可行，但寄存器压力待验证——每线程一次要持有 `EPI_N` 个 fp32 加 `EPI_N` 个 fp16）；A4 得 225 KB（账面贴边，但见下方风险）；A5 的 TMEM 需 768 列（超 512，否决，除非同时把 `blk_n` 降到 64）。

**预期结果**：脚本的基线输出应与 4.1.3 的手算一致；每个被否决的候选都对应一条「为什么不能天真地加深/加宽」的结论。特别地，A4（`BLK_K=128`）虽然账面可行，但存在机制风险：fp16 下 K 维一行 128 元素 = 256 字节，超过 SWIZZLE_128B 的 128 字节行宽（u6-l1/u6-l2），此时 `mma_shared_layout` 是否自动按多个 swizzle atom 分组、以及 MMA 描述符是否兼容，**待本地验证**（可在有 GPU 环境编译后用 `inspect_source()` 检查生成的 TMA 描述符与 `tcgen05` 指令形状）。这正是机制闸门存在的意义：资源账过了不等于能做。

#### 4.1.5 小练习与答案

**练习 1**：想把 `NUM_CONSUMER` 提到 3，资源闸门给出的结论是什么？有没有补救方案？
**答案**：TMEM 需 3 × 256 = 768 列 > 512，直接否决。补救方向是把每个消费者的列宽缩到 128（例如 `BLK_N=64` 使 `MMA_N=128`，3 × 128 = 384 ≤ 512），但 SMEM 账也要随之重算，且消费者越多，`mma2tma` 需要集齐的到达数越多（见 4.2），B 的复用收益与同步开销要重新权衡。

**练习 2**：为什么「PIPE_DEPTH 越深越好」在 Step 9 的块形下不成立？
**答案**：Step 9 每 stage 占 (2×128×64 + 128×64)×2 B = 48 KB，深度 4 已是 4×48+32+1 ≈ 225 KB，逼近 228 KB；深度 5 需 5×48+32+1 = 273 KB，直接超出每 SM 上限。书中 Step 7 一节同样算出深度 6 的 224 KB「几乎耗尽容量」（[chapter_gemm_advanced/index.md:L322-L323](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L322-L323)）。加深流水线必须与缩小 tile 或 Dsmem 联动。

**练习 3**：FA4 的 `rescale_threshold` 能不能提到 16？
**答案**：不能。阈值 \(\tau\) 决定保留旧参考时 P 的峰值可达 \(2^\tau\)，而 P 以 fp16 存入 TMEM，fp16 最大值 65504 < \(2^{16}\)，\(\tau=16\) 会让 P 溢出为 inf 进而产生 NaN。安全的上限在 15 以下且应留余量；当前值 8 对应峰值 256，非常安全（依据 [chapter_flash_attention/index.md:L74-L76](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L74-L76) 与 L326-L327 的 P fp16 事实）。

---

### 4.2 三要素设计文档：scope/layout/dispatch + 屏障协议

#### 4.2.1 概念说明

变体过了闸门之后、动代码之前，要写一份**设计文档**。它有两个用途：其一，写作过程强迫你把每个交接想清楚，绝大多数死锁与静默错果在这一步就能拦截；其二，一旦跑挂了，它就是调试附录里那张 roles/storage/handoff/lifetime 工作表的「应有版本」，与生成的 CUDA 对照即可定位偏差。调试附录原话就是「For any asynchronous kernel, make a small worksheet **before changing code**」（[appendix/debugging_warp_specialized.md:L30-L39](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/debugging_warp_specialized.md#L30-L39)），并明确同一张表适用于 GEMM 流水线和 FA4 的 QKᵀ/softmax/PV/校正交接（L49）。

本讲把这张表扩展成七节的设计文档：在 roles/storage/handoff/lifetime 四行之外，加上**命题与不变量**（改什么、什么必须保持等价）、**三要素变化表**（scope/layout/dispatch 各自变没变、怎么变）与**资源账**。这样一份文档同时是设计稿、调试对照表和代码评审材料。

#### 4.2.2 核心流程

文档的填写顺序与内核的执行结构对应：

```text
第 1 节 命题与不变量：一句话命题 + 收益假设（可证伪） + 数学不变量（如 D=ABᵀ 逐元素等价）
第 2 节 Roles（scope）：每个异步操作的发起者 = 哪个 CTA / warpgroup / warp / 单线程（守卫条件）
第 3 节 Storage（layout）：每类 tile 在每一步的驻留地（GMEM/SMEM/TMEM/RF）及其布局族
第 4 节 Handoff（屏障协议）：每道屏障的 类型/深度/期望到达数/初始相位/谁等谁到
第 5 节 Lifetime：每块存储最早何时可复用、何时必须释放
第 6 节 资源账：SMEM / TMEM / 寄存器 三行（4.1 的脚本输出）
第 7 节 验证与评测计划（指向 4.3）
```

填第 4 节时的三条规则（全部来自源码，不是本讲发明）：

1. **类型由完成者决定**：TMA 引擎完成 → `TMABar`（线程到达 + 字节账）；MMA 完成 → `TCGen05Bar`（`tcgen05.commit` 挂靠）；线程到达 → `MBarrier`（[chapter_gemm_advanced/index.md:L69](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L69)；FA4 侧见 [chapter_flash_attention/index.md:L290-L292](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L290-L292)）。
2. **到达数等于通知者数量**：多少个线程/引擎会到达，`init` 就写多少；多消费者时按消费者翻倍。
3. **初始相位按资源起点**：资源起始可用的一端（生产者的 empty 等待）给 1，起始不可用的一端（消费者的 full 等待）给 0（[chapter_gemm_advanced/index.md:L84-L89](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L84-L89)）。

#### 4.2.3 源码精读

**Roles 表（scope）的范本。** Step 9 的角色表是现成的第 2 节模板：WG2 的 warp 0 / warp 1 分别是两个 MMA 发起 warp（CTA 0 中被 `elect_sync` 选中的单线程发起），warp 3 是 TMA producer，WG0/WG1 全体做回写（[chapter_gemm_advanced/index.md:L619-L631](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L619-L631)）。落到代码里，角色由 `wg_id`/`warp_id` 分支选择，MMA 消费者 warp 用 `warp_id` 选自己的 A 块与 TMEM 列区间（[chapter_gemm_advanced/index.md:L789-L811](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L789-L811)），回写 warpgroup 用 `wg_id` 选同一个槽（[chapter_gemm_advanced/index.md:L813-L851](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L813-L851)）。

**Storage（layout）的变化方式。** 加第二个消费者时 `Asmem` 增加一维 `NUM_CONSUMER`，布局对象同步改（[chapter_gemm_advanced/index.md:L637-L645](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L637-L645)），TMEM 则切成 `[0:256]` 与 `[256:512]` 两段（L653）。这就是第 3 节要写的内容：**每加一个复用维度，哪些 buffer 加轴、哪些切片重划**。

**Handoff（屏障协议）的重算。** Step 9 的屏障初始化块是第 4 节的直接依据：

[chapter_gemm_advanced/index.md:L722-L727](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L722-L727)：`tma2mma.init(1)`（单线程登记字节账）、`mma2tma.init(NUM_CONSUMER)`（**两个消费者都读完才能释放 stage**，到达数从 Step 8 的 1 变 2）、`mma2ld.init(1)`、`ld2mma.init(128 * CTA_GROUP)`（两 CTA 共 256 个回写线程到达）。同时 `expect_tx` 字节数要乘上 `CTA_GROUP * (NUM_CONSUMER * BLK_M*BLK_K + BLK_N*BLK_K) * F16_SIZE`（[chapter_gemm_advanced/index.md:L783-L785](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L783-L785)）。**任何一个变体只要改变了「谁搬运、搬多少、谁消费」，这三处数字都要重算**——漏算的后果在 u12-l1/u13-l2 已经推演过：少登字节静默错果，多登字节内核挂死。

**更复杂的协议范本（FA4）。** FA4 的屏障表（[chapter_flash_attention/index.md:L296-L309](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L296-L309)）给出了每道屏障的「参与通知的线程数 / 一个相位完成的条件 / 完成后什么变得安全」三列，例如 `p_o_rescale` 要集齐 256 次到达（128 个 softmax 线程 + 128 个 WG2 线程）。若你的变体走 B 轨（FA4），第 4 节应逐行继承这张表再标注改动。

**Lifetime 的验证方法。** 调试附录给出了与生成 CUDA 对照的检查清单：角色守卫与 roles 表一致、屏障 init 出现在角色分支之前、集体操作没有被 lane/warp/warpgroup 守卫意外缩小、arrive/wait 相位与 handoff 表一致、TMA store 排空与 TMEM 释放只在 lifetime 表允许的时机发生（[appendix/debugging_warp_specialized.md:L41-L49](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/debugging_warp_specialized.md#L41-L49)）。这份清单就是设计文档写完后的第一次「纸面评审」。

#### 4.2.4 代码实践：为你的变体填全设计文档

**实践目标**：从下面两个方向任选其一，产出一份完整的七节设计文档（Markdown 表格即可），并与源码逐行核对。

**操作步骤**：

1. 先把 `hgemm_v9` 的文档填出来（这是「标准答案」，用来校准你的填表粒度）：
   - Roles 表照抄 [L619-L631](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L619-L631) 的五行，但补上守卫条件列（`wg_id==2`、`warp_id<NUM_CONSUMER`、`cbx==0`、`T.filter(lane_id, elect_sync())`）。
   - Handoff 表照 [L722-L727](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L722-L727) 抄五道屏障的类型/深度/到达数，并补上初始相位列（对照 [L84-L89](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L84-L89) 的规则与代码里 `PipelineState(PIPE_DEPTH, phase=...)` 的实参）。
2. 再为你在 4.1.4 中选定的变体填新文档，**只允许出现差异行**，例如选 A3（`PIPE_DEPTH=3, EPI_N=128`）：
   - 资源账行更新为 209 KB；
   - Handoff 表中所有 `PIPE_DEPTH` 相关的屏障深度从 4 改 3；
   - Storage 表中 `Dsmem` 形状从 `(2,128,64)` 改 `(2,128,128)`；
   - Roles 表不变（这正是「一次只改一组联动」的意义）；
   - Lifetime 表新增一条：每线程寄存器同时存活 `EPI_N` 个 fp32 + fp16，128 时是否触及寄存器上限——**待本地验证**（先编译，若失败按调试附录的「Buffer scope / 编译失败」表排查）。
3. 用 [appendix/debugging_warp_specialized.md:L41-L49](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/debugging_warp_specialized.md#L41-L49) 的清单对文档做一次纸面评审。

**需要观察的现象**：填表过程中你会被迫回答一些读代码时容易滑过去的问题——例如「`mma2tma` 的第二次到达来自哪个 warp 的哪行代码」「回写分支里为什么必须用 `warpgroup_sync(wg_id+10)` 而不是 `cta_sync`」。若某一格填不出来，说明你对这个交接的理解还不完整，先不要动代码。

**预期结果**：两份文档（基线 + 变体差异）。有 GPU 时，后续 4.3 的验证与调试都以它为对照表；无 GPU 时，它本身就是本讲要求的「完整推演」的主体。

#### 4.2.5 小练习与答案

**练习 1**：把 `hgemm_v9` 退回 Step 8（`NUM_CONSUMER` 2→1），设计文档的哪些行必须改？
**答案**：Roles 表删去 WG2 warp 1 与 WG1 两行；Storage 表 `Asmem`/`Dsmem` 去掉 `NUM_CONSUMER` 维（布局对象同步改）；Handoff 表 `mma2tma.init(1)`、`mma2ld`/`ld2mma` 深度回到 1、`expect_tx` 去掉 `NUM_CONSUMER` 因子；调度器 `num_m_tiles = M // 256`（[chapter_gemm_advanced/index.md:L659-L666](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L659-L666)）；回写从两个 warpgroup 退回一个（命名屏障 ID 从 `wg_id+10` 退回 10）；资源账重算。dispatch 全程不变。

**练习 2**：为什么 `tma2mma.init(1)` 而 `mma2tma.init(NUM_CONSUMER)`，两个方向的到达数逻辑有何不同？
**答案**：`tma2mma` 的完成条件是「一次线程到达 + 登记的字节全部送达」，到达由 `cbx==0` 一侧的单个登记线程贡献，故期望线程数为 1，数据就绪由字节账保证；`mma2tma` 是资源归还屏障，stage 里的 A/B 要等**每一个**消费者都读完才能释放，两个消费者 warp 各自经 `tcgen05.commit` 贡献一次到达，故期望到达数为 `NUM_CONSUMER`（依据 [chapter_gemm_advanced/index.md:L655-L657](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L655-L657)）。

**练习 3**：变体新增一道「资源起始就被占用」的 empty 屏障，初始相位应取 0 还是 1？为什么？
**答案**：取 0。初始相位的规则是「资源起始可用的一端给 1、不可用的一端给 0」：empty 屏障的等待方（生产者）在内核启动时该缓冲仍被（概念上的）上一轮占用，第一次 `wait(phase=0)` 必须阻塞，直到消费者真正归还；若误给 1，首次等待立即通过，生产者会覆写尚未消费的数据（依据 [chapter_gemm_advanced/index.md:L84-L89](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L84-L89)，死锁/错果两种故障模式已在 u8-l2 推演）。

---

### 4.3 验证与评测方案：从数值断言到归因报告

#### 4.3.1 概念说明

变体实现之后的工作分两段，附录反复强调不可混在一起：**测量回答「多快」，诊断回答「时间去哪了」**（[appendix/benchmarking_gpu_kernels.md:L15-L27](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/benchmarking_gpu_kernels.md#L15-L27)）。本讲的验证与评测方案由三段构成：

1. **正确性段**：计时之前先在声明容差下与更精确的参考对照，失败即终止（[appendix/benchmarking_gpu_kernels.md:L28-L62](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/benchmarking_gpu_kernels.md#L28-L62)）。
2. **计时段**：显式声明计时边界（一次被测操作涵盖哪些内核与拷贝，[appendix/benchmarking_gpu_kernels.md:L63-L91](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/benchmarking_gpu_kernels.md#L63-L91)），用 CUDA events 测流内时间（L92 起），保持条件一致（锁定时钟、交替测量顺序等，L338-L370），再按 \(\text{TFLOP/s} = 2MNK/t\) 换算吞吐（L371-L391）。
3. **归因段**：把变体与基线的差值归到机制，只在诊断时用 Proton/Nsight/NCU/IKET，且报告数字必须回到未剖析的 CUDA events 基线（u15-l5、u15-l6）。

对没有 Blackwell GPU 的读者，这一模块替换为「源码推演 + 风险清单」：结论不写成「变体快了 X%」，而写成「变体改变机制 M，预期指标 I 向方向 D 移动，风险点为 R」。

#### 4.3.2 核心流程

```text
【有 GPU 路径】
0. 环境自检：tvm.__version__ / tvm.__file__、目标为 cuda、dispatch 受支持
1. 正确性：小形状先跑 → 参考实现升 fp32 → assert_close(声明容差) → 打印 max_err 再断言
2. 计时：warmup 至稳态 → CUDA events 包住被测区间 → 多迭代取中位数
3. 条件：锁定时钟、同形状同 dtype、基线与变体交替测量
4. 归因：与基线做同条件对比 → 差值落到机制 → 必要时 NCU/IKET 验证假设
【无 GPU 路径】
1. 推演：资源账 + 三要素变化表 + 屏障协议逐行核对
2. 风险清单：按「死锁 / 崩溃 / 错误结果 / 正确但慢」四类预登记风险与排查入口
3. 预测：给出方向性预期与可证伪的判据（如「若 NCU 显示 L2 命中率不升则假设被否证」）
```

#### 4.3.3 源码精读

**正确性段的完整范本（FA4）。** FA4 一章的验证示例可直接套用到任何变体：先断言输入满足硬约束（`GQA_RATIO` 整除、`HEAD_DIM=128`、non-causal 下 `SEQ_LEN_KV` 被 128 整除，[chapter_flash_attention/index.md:L863-L868](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L863-L868)），再编译、执行、与 `F.scaled_dot_product_attention` 的 fp32 参考对照并断言 `rtol=1e-2, atol=1e-2`（[chapter_flash_attention/index.md:L872-L899](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L872-L899)）。尤其值得学习的是它对「误差偏大」的归因指引：容差覆盖的是有限精度效应（fp16 存储与舍入、exp2 与多项式近似的精度、分块累加顺序、最终 fp16 转换），**超出容差的误差通常指向 softmax 交接屏障**——缺 `s_ready`/`p_o_rescale`/`p_ready_2` 等待，或 `row_max`/`row_sum` 未送达校正路径（[chapter_flash_attention/index.md:L902-L904](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L902-L904)）。变体验证脚本应把这段「预期误差来源 + 超差先查哪」写进注释。

**计时段的条件声明范本（GEMM）。** 九步性能表开头的条件声明是标准写法：NVIDIA B200、`M=N=K=4096`、fp16 输入、锁定时钟、每个受测版本 1000 次计时迭代（[chapter_gemm_advanced/index.md:L862-L864](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L862-L864)），并明确这些数字「用于同条件版本间比较，不代表其他问题规模或环境下的峰值性能」（L883）。你的变体报告应原样照抄这套声明。

**归因段的比较区间范本。** 九步表只认四个比较区间，每个区间把增益落到具体机制（1→4 约 142×含 K 循环/分块/多 CTA/TMA 的合计；4→7 约 2.2×来自流水线+持久调度+warp 特化；7→8 约 2.2×来自双 CTA 复用；8→9 约 10% 来自第二个消费者共享 B，[chapter_gemm_advanced/index.md:L885-L890](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L885-L890)）。变体的归因也应写成这种「单一区间 + 单一机制」的形式，避免把多个改动的合力记到一个机制头上。

**无 GPU 路径的排查入口。** 调试工作流的八步（最小形状复现 → 先查环境与编译 → 保存 `inspect_source("cuda")` → 写工作表 → 对照生成 CUDA → 症状分类 → 一次只改一处交接 → 先正确性后性能）与编译失败速查表（[appendix/debugging_warp_specialized.md:L19-L28](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/debugging_warp_specialized.md#L19-L28)、[L51-L60](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/debugging_warp_specialized.md#L51-L60)）是风险清单里每一条的「排查入口」列的内容来源。

#### 4.3.4 代码实践：验证与计时脚本（或风险清单）

**实践目标**：为你的变体写一个 `verify_and_bench.py`（有 GPU），或一份「推演 + 风险清单」（无 GPU）。

**操作步骤**（以下脚本骨架为示例代码，需按你的变体补全）：

```python
# 示例代码：verify_and_bench.py 骨架（GEMM 轨；FA4 轨对照 chapter_flash_attention 的验证示例）
import torch, tvm
# from my_variants import hgemm_v9_variant   # 你的变体，必须写在文件里（TIRx 依赖源码检视）

M = N = K = 4096
torch.manual_seed(0)
A = torch.randn(M, K, dtype=torch.float16, device="cuda")
B = torch.randn(N, K, dtype=torch.float16, device="cuda")
D = torch.empty(M, N, dtype=torch.float16, device="cuda")

# kernel = hgemm_v9_variant(M, N, K)
# ex = tvm.compile(tvm.IRModule({"main": kernel}), target="cuda", tir_pipeline="tirx")
# ex.mod(A, B, D); torch.cuda.synchronize()

# 1) 正确性先行：参考实现升 fp32，打印 max_err 再断言（容差与书中一致）
ref = (A.float() @ B.float().T).half()
# max_err = (D.float() - ref.float()).abs().max().item(); print("max_err =", max_err)
# torch.testing.assert_close(D, ref, rtol=2e-2, atol=1e-2)

# 2) 计时：CUDA events，warmup 后多轮取中位数（协议见附录 L92-L204）
# starts/ends 成对创建；条件：锁定时钟、与基线交替测量、声明计时边界只含这一个内核
# 3) 报告：ms 中位数 + TFLOPS = 2*M*N*K / t；注明 B200、形状、dtype、迭代数
```

无 GPU 时，把下面这张风险清单逐条落到你的变体上（每条给「是否适用 / 排查入口」两列）：

| # | 风险 | 症状类别 | 排查入口 |
|---|------|----------|----------|
| R1 | 初始相位设错（full/empty 起点颠倒） | 死锁或静默错果 | 对照 [L84-L89](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L84-L89) 的规则逐道核对 |
| R2 | `expect_tx` 字节数漏乘 `CTA_GROUP`/`NUM_CONSUMER` | 静默错果（少登）/ 挂死（多登） | 重算公式 [L783-L785](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L783-L785)；u12-l1/u13-l2 |
| R3 | 屏障期望到达数未随角色数重算（如 `mma2tma`、`ld2mma`） | 死锁 | 核对 init 块 [L722-L727](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_advanced/index.md#L722-L727) |
| R4 | 集体操作被守卫缩小（如回写分支内误用 `cta_sync`） | 死锁 | u15-l2 守卫集合检查法 |
| R5 | SMEM 超出 228 KB | 启动失败 | 4.1.4 资源账脚本 |
| R6 | `BLK_K` 超过 swizzle 行宽（fp16 下 64 元素/128 B） | 编译失败或布局错配 | `inspect_source()` 查 TMA 描述符；u6-l1/u6-l2 |
| R7 | M、N 不被新块尺寸整除 | 越界/错果 | 命题阶段加整除断言（对照 FA4 的约束清单 L863-L868） |
| R8 | 中间量数值范围超出存储 dtype（如 FA4 的 P 超出 fp16 表示域） | inf/NaN | 4.1 数值域分析 |
| R9 | 非法访存毒化 CUDA context | 后续运行全挂 | 重启 Python 再测（调试附录 L145 起） |
| R10 | 剖析数字被当成基准报告 | 结论失真 | 一切延迟数字回到未剖析的 CUDA events 基线（u15-l5/u15-l6） |

**需要观察的现象**：有 GPU 时——正确性段的 `max_err` 应落在容差内；计时段的多次运行中位数应稳定（波动明显说明时钟未锁或存在其他干扰）；变体与基线的差值应能落到你在设计文档里写的收益假设上。无 GPU 时——风险清单应覆盖 R1–R10 中所有适用项，每项都能指到一个具体的核对位置。

**预期结果**：有 GPU 路径产出「PASS 输出 + 计时表 + 归因一段」；无 GPU 路径产出「推演文档 + 风险清单 + 方向性预测」。**本讲所有脚本输出均为待本地验证**（编写环境无 Blackwell GPU，我们不假装已经运行过）。

#### 4.3.5 小练习与答案

**练习 1**：变体测得 0.090 ms、基线 0.094 ms，如何报告这个结果？
**答案**：按九步表的条件格式报告——同一 B200、同形状同 dtype、锁定时钟、同迭代数、CUDA events、取中位数；先给正确性 PASS，再给两个中位数与波动幅度；明确声明「仅构成同条件版本间比较」。4% 的差距必须对照多次运行的波动量级解读，若波动同量级则结论应写「未观察到显著差异」而不是「提速 4%」。

**练习 2**：FA4 轨变体数值误差明显超出 `rtol=1e-2, atol=1e-2`，第一 suspects 是什么？
**答案**：softmax 交接屏障——缺 `s_ready`、`p_o_rescale` 或 `p_ready_2` 的等待，或 `row_max`/`row_sum` 更新未送达校正路径；而不是先去怀疑算法公式（依据 [chapter_flash_attention/index.md:L902-L904](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L902-L904)）。

**练习 3**：NCU 显示变体的 occupancy 低于基线，但变体更快，这矛盾吗？
**答案**：不矛盾。occupancy 是驻留并发度而非性能度量；现代 Tensor Core 内核常故意以低 occupancy 换显式重叠（u3-l3），NCU 各吞吐指标各自以自身峰值为分母、不可相加（u15-l5）。判据应是关键硬件单元（Tensor Core、TMA）的活跃度与空闲原因，而不是 occupancy 本身。

---

## 5. 综合实践

**Capstone 任务：从 `hgemm_v9` 或 FA4 出发，完成一个自定义变体的完整闭环。**

**任务描述**。从下面的双轨中任选，或自拟（自拟需通过 4.1 的四道闸门）：

- **A 轨（GEMM）建议命题**：
  - A3：`PIPE_DEPTH=3, EPI_N=128` 的联动改动（资源账 209 KB，寄存器压力待验证）；
  - A4：调度参数重调——`l2_group_size` 取 4/8/16 扫一遍，用 NCU 的 L2 命中率验证收益假设（零资源风险，适合作为第一次 capstone）；
  - A5：`BLK_K=128, PIPE_DEPTH=2`（账面可行但 swizzle 宽度风险高，适合想深入 TMA 描述符的读者）；
  - 自拟：把 Step 6/7 的静态调度换成 CLC 动态认领（u8-l3），预测并测量尾部空闲的变化。
- **B 轨（FA4）建议命题**：
  - B1：`rescale_threshold` 取 4/8/12 三个值，统计校正路径触发次数与总时间的变化（注意 4.1.5 练习 3 的 fp16 上限）；
  - B2：为变长序列（`SEQ_LEN_KV` 非 128 倍数）的 non-causal 路径补尾块掩码——对照 [chapter_flash_attention/index.md:L863-L868](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L863-L868) 指出的「不做尾掩码」现状，这是一个真实的贡献机会。

**交付物**（缺一不可）：

1. **D1 设计文档**：4.2 的七节结构，含资源账脚本输出与三要素变化表；
2. **D2 实现**：参数补丁或内核代码，附「相对基线的最小 diff」说明（一次只改一组联动）；
3. **D3 验证**：GPU 上给出 PASS 输出与 `max_err`；无 GPU 给出完整推演 + 覆盖 R1–R10 的风险清单；
4. **D4 评测**：GPU 上给出同条件计时表（基线 vs 变体，含条件声明）与归因一段；无 GPU 给出方向性预测与可证伪判据；
5. **D5 报告**：一页总结——命题、机制、结果（或推演结论）、失败与回退记录。所有「待本地验证」项在报告中单独列出。

**验收自查表**：

- [ ] 命题能落到九步表的两条主线之一（别让 Tensor Core 等数据 / 每片数据多算几次）？
- [ ] 资源账三张（SMEM/TMEM/寄存器）都算过且有脚本可复算？
- [ ] 每道被改动的屏障都重算了类型、深度、到达数与初始相位？
- [ ] 正确性先于一切计时；容差与误差来源写明？
- [ ] 结论只主张「同条件版本间比较」，剖析数字没有混进基准报告？

完成 A4 或 B1 这类低风险命题即算达标；A5/B2 属于进阶，后者有机会成为对本书的真实贡献（README 明确欢迎 corrections 与 examples，[README.md:L13-L14](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/README.md#L13-L14)）。

## 6. 本讲小结

- 变体设计的自由度是显式可枚举的：`hgemm_v9` 头部的常量块就是机制菜单（tile 形状、协作与复用、调度、精度、算法/掩码五条轴）。
- 立项先过四道闸门：资源账（SMEM ≤ 228 KB、TMEM ≤ 512 列）、正确性（整除 + 数值表示域 + 数学等价）、机制兼容（swizzle 宽度、MMA 形状）、可证伪的收益假设；Step 9 基线本身已贴 SMEM 天花板（≈225 KB），多数「加宽加深」的朴素想法在账面上就死。
- 设计文档 = 调试工作表的扩展：命题与不变量、roles/scope、storage/layout、handoff（屏障类型/深度/到达数/初始相位）、lifetime、资源账、验证计划七节；三条填表规则全部来自源码——屏障类型由完成者决定、到达数等于通知者数量、初始相位按资源起点。
- 任何改变「谁搬运、搬多少、谁消费」的变体都要联动重算 `expect_tx` 字节数与屏障到达数：少登静默错果，多登内核挂死。
- 验证与评测分三段：正确性先行（容差 + 误差来源声明）、CUDA events 计时（条件一致性 + 吞吐换算）、单区间单机制的归因；剖析数字只用于诊断，报告一律回到未剖析基线。
- 无 Blackwell GPU 时整个 capstone 走「资源账 + 三要素推演 + 风险清单 + 方向性预测」路径，同样能产出完整的工程结论。

## 7. 下一步学习建议

- **读真实内核仓库**：安装并通读 [tirx-kernels](https://github.com/mlc-ai/tirx-kernels)（README 指定的配套 revision），重点看 `tirx_kernels/attention/flash_attention4.py` 全文——本手册 FA4 各讲只节选了它的关键片段，capstone B 轨的实现工作应以它为底本。
- **深挖两条进阶机制**：block-scaled 混合精度（MXFP8/NVFP4 的 SFA/SFB 布局与 `tcgen05.cp`，u5-l3、u7-l2）是 A 轨精度类变体的下一步；CLC 动态调度（u8-l3）是调度类变体的天花板。
- **向本书贡献**：B2（non-causal 尾块掩码）或一个新的变体评测章节都是合适的 PR 素材；贡献前重读 u1-l2 的构建约定（CI 两轮警告即失败）并在本地 `sphinx-build` 通过后再提交。
- **把方法论迁移出去**：capstone 的「四道闸门 + 七节文档 + 三段验证」不依赖 TIRx，可直接用于你在 CUTLASS、Triton 或其他 DSL 上的内核开发。
