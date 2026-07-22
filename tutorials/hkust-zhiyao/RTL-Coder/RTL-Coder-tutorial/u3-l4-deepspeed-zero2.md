# DeepSpeed ZeRO-2 与分布式训练

## 1. 本讲目标

本讲专门解决一个问题：**RTL-Coder 用 6.7B 参数的底座模型（DeepSeek-coder / Mistral）做微调，单张 GPU 根本放不下，怎么靠一份 42 行的 DeepSpeed 配置把它跑起来？**

学完后你应该能够：

1. 说清 **ZeRO Stage 2** 到底切分了训练状态里的哪一部分、保留了哪一部分，以及为什么是「省显存」与「少通信」之间的折中。
2. 看懂 `offload_optimizer` 把优化器状态搬到 CPU 内存、并用 `pin_memory` 加速传输的原理与代价。
3. 解释 **fp16 动态 loss scale** 各参数的含义，理解它为什么能避免梯度下溢。
4. 读懂 `torchrun --nproc_per_node=4` 的多进程启动方式，以及配置里大量 `"auto"` 字段是如何被命令行参数回填的。
5. 论证一个贯穿全讲的结论：**评分训练（`mle_scoring.py`）比普通 SFT（`mle.py`）更依赖 CPU offload**，并能把这条结论与 u3-l1、u3-l3 的算法侧省显存方案连起来。

## 2. 前置知识

在进入配置之前，先用三段话把「分布式训练为什么吃显存」讲透。这是理解 ZeRO 的地基。

**训练状态的三份副本。** 用 AdamW + fp16 混合精度训练一个因果语言模型时，每个参数 Ψ 在每张 GPU 上要存三样东西：

| 组成 | 精度 | 每参数字节 | 说明 |
| --- | --- | --- | --- |
| 模型参数 | fp16 | 2 | 前向/反向实际用的「工作副本」 |
| 梯度 | fp16 | 2 | 反向传播产出 |
| 优化器状态 | fp32 | 12 | fp32 主权重(4) + Adam 一阶矩(4) + 二阶矩(4) |

把三行加起来，纯数据并行（standard data parallel）下每张卡都要复制完整的一份：

\[
M_{\text{base}} = (2+2+12)\Psi = 16\Psi
\]

**为什么放不下。** RTL-Coder 的底座是 6.7B 参数（Ψ≈6.7×10⁹）。代入上式：

\[
16\Psi \approx 16 \times 6.7\text{e9} \approx 107\text{ GB}
\]

一张卡要 107 GB——这还没算激活值（activation）。所以「直接 `model.to('cuda')` 然后开训」在 6.7B 上是死路一条，必须把训练状态切片。

**ZeRO 的核心思想。** ZeRO（**Z**ero **R**edundancy **O**ptimizer）观察到：数据并行下每张卡都存了**一模一样**的完整参数/梯度/优化器状态，这是「冗余」。ZeRO 按阶段把这些状态在卡间切分（partition），用到时再通信拼回来。三个阶段切的东西越来越多、省得越来越多、但通信也越来越重。本讲只关心 Stage 2。

> 名词速查：**激活值（activation）**是前向过程中每一层产生的中间张量，反向传播算梯度时要重新用到。激活值大小正比于 `batch × 序列长度`，是 SFT 与评分训练显存差异的关键（见第 5 节）。

本讲承接 u2-l7（`mle.py` 标准 SFT 的数据流），并为 u3-l1 / u3-l3 的评分训练省显存问题提供**系统侧**的答案——评分训练除了算法侧的梯度切分，还要靠本讲的 ZeRO-2 + CPU offload 才能跑得动。

## 3. 本讲源码地图

本讲只涉及两个文件，但会反复对照 README 里的三条启动命令：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `train/ds_stage_2.json` | DeepSpeed 配置，三种训练方案共用 | fp16 / optimizer / zero_optimization / `"auto"` 字段 |
| `README.md` | 给出三条 `torchrun` 启动命令 | 命令行参数如何回填配置的 `"auto"` |

辅助参照（不展开）：

| 文件 | 作用 |
| --- | --- |
| `train/mle.py` | 标准 SFT，继承 HF `TrainingArguments`，消费本配置 |
| `train/mle_scoring.py` | 评分训练，`per_device_train_batch_size=1`，更吃显存 |
| `train/mle_scoring_grad_split.py` | 评分训练 + 梯度切分，从算法侧进一步省显存 |

## 4. 核心概念与源码讲解

### 4.1 ZeRO Stage 2：优化器状态与梯度的卡间切分

#### 4.1.1 概念说明

ZeRO 把训练状态切成三块（参数、梯度、优化器状态），按切分粒度分三档：

- **Stage 1**：只切**优化器状态**。
- **Stage 2**：切**优化器状态 + 梯度**。
- **Stage 3**：切**优化器状态 + 梯度 + 参数**（三块全切）。

切得越深越省显存，但通信也越频繁：Stage 3 要在前向/反向的每一层都 all-gather 把参数临时拼回来，通信开销最大。**Stage 2 是「省显存」与「少通信」的甜点**——它把最胖的优化器状态（12Ψ）和梯度都切了，但参数仍然完整留在每张卡上，前向/反向不需要为参数做额外通信，因此速度接近纯数据并行。RTL-Coder 选的就是 Stage 2。

#### 4.1.2 核心流程

设 GPU 数为 \(N_d\)，ZeRO Stage 2 下每张卡的训练状态显存为：

\[
M_{\text{ZeRO-2}} = \underbrace{2\Psi}_{\text{参数（不切）}} + \underbrace{\frac{2\Psi}{N_d}}_{\text{梯度（切分）}} + \underbrace{\frac{12\Psi}{N_d}}_{\text{优化器状态（切分）}} = 2\Psi + \frac{14\Psi}{N_d}
\]

执行流程伪代码：

```text
每张卡 rank r 持有：完整参数(2Ψ) + 梯度的一个分片(2Ψ/Nd) + 优化器状态的一个分片(12Ψ/Nd)

forward:  各卡用自己的完整参数前向（无需通信，这就是 Stage2 的省通信之处）
backward: 各卡算出完整梯度 → reduce-scatter：每卡只保留自己负责的那个分片
                   （其余分片在 reduce 后即丢弃，省下 (Nd-1)/Nd 的梯度显存）
step:     每卡只用「自己的优化器状态分片」更新「自己负责的那一段参数」
          → 下一次 forward 前，无需 all-gather 参数（参数本来就是完整的）
```

对比 Stage 3：Stage 3 把参数也切了，所以 forward 前必须 all-gather 把参数临时拼回来，反向后还要再切回去——通信量明显增加。Stage 2 之所以「快」，正是因为它**故意不切参数**。

#### 4.1.3 源码精读

配置里的 stage 声明只有一行：

[train/ds_stage_2.json:L22-L34](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/ds_stage_2.json#L22-L34) —— `"stage": 2` 即开启 Stage 2；后面 4.2 节会读 `offload_optimizer`，4.1 先看其余通信优化字段：

- `"allgather_partitions": true` 与 `"reduce_scatter": true`：Stage 2 的核心开关——`reduce_scatter` 让梯度在反向后被「归约并分散」，每卡只留自己分片，对应 4.1.2 里 backward 的关键步骤。
- `"allgather_bucket_size": 2e8` / `"reduce_bucket_size": 2e8`：通信按 2×10⁸ 个元素（约 200M 个 float）打成「桶」批量收发，而不是逐层逐张量零散发送，从而摊薄通信启动开销、并让通信能与计算重叠。2e8 ≈ 0.8 GB（fp32）。
- `"overlap_comm": true`：让「下一层的计算」与「当前层的梯度 reduce-scatter」时间上重叠，填掉通信气泡。
- `"contiguous_gradients": true`：把梯度分配在**连续内存**里再归约，避免碎片化、加速集合通信。

这些都是「把 Stage 2 的通信代价压到最低」的工程旋钮，不影响切分逻辑本身。

#### 4.1.4 代码实践

**实践目标**：用一段纯 Python 把 ZeRO-2 相对纯数据并行的显存收益算出来，建立数量级直觉（无需 GPU）。

**操作步骤**（示例代码，可本地运行）：

```python
# 示例代码：估算 ZeRO-2 显存（不含激活，仅训练状态）
Psi = 6.7e9          # 6.7B 参数
Nd   = 4             # 4 张 GPU
GB   = 1024**3

M_base  = 16 * Psi                       # 纯数据并行，每卡
M_zero2 = 2*Psi + (14*Psi)/Nd            # ZeRO Stage 2，每卡

print(f"纯数据并行  每卡: {M_base/GB:6.1f} GB")
print(f"ZeRO-2     每卡: {M_zero2/GB:6.1f} GB  (省 {(M_base-M_zero2)/GB:.1f} GB)")
```

**需要观察的现象**：纯数据并行 ≈ 107 GB/卡，ZeRO-2 ≈ 37 GB/卡，单靠切分就省下约 70 GB。

**预期结果**：ZeRO-2 把每卡训练状态从 16Ψ 压到约 2Ψ+14Ψ/4 ≈ 36.9 GB；但 37 GB 仍可能逼近单卡上限（尤其加上激活），这正是下一节「再搬走优化器状态」的动机。

> 待本地验证：以上是粗略上界估算，真实占用还含 CUDA context、kernel 临时缓冲、激活值；以 `nvidia-smi` 实测为准。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `stage` 从 2 改成 3，每卡训练状态显存的理论值是多少？代价是什么？

**参考答案**：Stage 3 三块全切，每卡 \(M = 16\Psi/N_d\)。4 卡下约 26.8 GB，比 Stage 2 更省；但前向/反向每一层都要 all-gather 参数、反向后切回，通信量显著增大，训练更慢。

**练习 2**：为什么 RTL-Coder 不直接用 Stage 3 把显存压到最低？

**参考答案**：6.7B 模型在 Stage 2 + CPU offload（见 4.2）后已经能放进常见 GPU；Stage 3 虽然更省，但逐层 all-gather 参数带来的通信与算子拆分开销会拖慢训练。对「参数量中等、想兼顾速度」的场景，Stage 2 是更优折中。

---

### 4.2 offload_optimizer：把优化器状态搬到 CPU（pin_memory）

#### 4.2.1 概念说明

4.1 算出 ZeRO-2 每卡仍有约 37 GB，其中优化器状态分片占了 \(12\Psi/N_d\)（4 卡约 20 GB）。`offload_optimizer` 把这块**最胖的分片从 GPU 搬到 CPU 内存**，GPU 侧就只剩参数 + 梯度分片：

\[
M_{\text{ZeRO-2+offload}}^{\text{GPU}} \approx 2\Psi + \frac{2\Psi}{N_d}
\]

对 6.7B / 4 卡约 16.8 GB——腾出的 ~20 GB 全留给激活值，这正是大 batch、长序列、多候选训练能跑得动的关键。代价是：Adam 更新改在 CPU 上做（用 DeepSpeed 的高度优化版 `DeepSpeedCPUAdam`），且每步要在 CPU↔GPU 之间搬梯度/参数更新，引入 PCIe 传输延迟。属于**用时间换空间**。

#### 4.2.2 核心流程

```text
backward 后：
  梯度分片(2Ψ/Nd, GPU) ──copy──> CPU
optimizer step（在 CPU 上）：
  用 CPU 上的优化器状态分片(12Ψ/Nd) 更新 → 得到参数更新量
  参数更新量 ──copy──> GPU，更新对应参数分片
```

`pin_memory: true` 的作用：分配的是**页锁定内存（page-locked / pinned memory）**。普通 CPU 内存可被操作系统换出到磁盘，DMA 传输时要先「钉住」；pinned 内存常驻物理页、不可换出，GPU 可直接对其做 DMA，传输带宽显著更高，且能与 GPU 计算**异步重叠**。代价是 pinned 内存是稀缺的 OS 资源、分配更慢，所以只对需要频繁 H2D/D2H 搬运的优化器状态开启。

#### 4.2.3 源码精读

[train/ds_stage_2.json:L24-L27](https://github.com/hkust-zhiyao-RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/ds_stage_2.json#L24-L27) —— 就这两行决定了 offload 行为：

- `"device": "cpu"`：优化器状态分片放在 CPU 内存（若改为 `"none"` 或删掉整个 `offload_optimizer` 段则关闭 offload，优化器状态回到 GPU）。
- `"pin_memory": true`：用 pinned 内存加速 CPU↔GPU 传输。

> 进阶细节：当 `device: "cpu"` 时，DeepSpeed 自动启用其自带的 `DeepSpeedCPUAdam`（指令级优化的 CPU Adam），而不是 PyTorch 原生的 CPU Adam——否则 CPU 端的优化器步会成为严重瓶颈。这也是为什么配置里显式写了 `"optimizer": {"type": "AdamW"}`（见 [train/ds_stage_2.json:L11-L19](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/ds_stage_2.json#L11-L19)），DeepSpeed 会据此构造正确的 CPU 优化器，覆盖 `train/mle.py` 里 `TrainingArguments` 的 `optim="adamw_torch"` 默认值（见 [train/mle.py:L43-L49](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle.py#L43-L49)）。

#### 4.2.4 代码实践

**实践目标**：亲手关掉 optimizer offload，预测显存与速度的变化方向（配置编辑 + 预测，本机无需多卡）。

**操作步骤**：

1. 复制配置：`cp train/ds_stage_2.json train/ds_stage_2_no_offload.json`（**不要改原文件**）。
2. 在副本里删除 `offload_optimizer` 整段（即删掉 [train/ds_stage_2.json:L24-L27](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/ds_stage_2.json#L24-L27) 这 4 行），或把 `"device"` 改成 `"none"`。
3. 用 `python -c "import json;print(json.dumps(json.load(open('train/ds_stage_2_no_offload.json')),indent=2))"` 确认 JSON 仍合法。

**需要观察的现象**（思维实验，需多卡实测验证）：

| 维度 | 关闭 offload 后的变化 |
| --- | --- |
| GPU 显存 | **上升**约 \(12\Psi/N_d\)（4 卡、6.7B ≈ +20 GB），评分训练大概率 OOM |
| CPU 内存 | **下降**（优化器状态不再驻留 CPU） |
| 单步速度 | **变快**（省掉 CPU 上的 Adam 步与 PCIe 搬运），前提是没 OOM |

**预期结果**：对 `mle.py`（batch=2、单序列）可能仍跑得动且更快；对 `mle_scoring.py`（batch=1、但每样本 N 个候选）极可能直接 OOM——这正是 4.2.5 要回答的问题。

> 待本地验证：上述为方向性预测，实际是否 OOM 取决于 `model_max_length`、候选数 N、是否开 `gradient_checkpointing`。

#### 4.2.5 小练习与答案

**练习 1**：`pin_memory: true` 能加速什么？它为什么不能对所有张量都开？

**参考答案**：它让优化器状态用 pinned 内存，加速 CPU↔GPU 的 DMA 拷贝并支持异步重叠。pinned 内存常驻物理页、不可换页、是稀缺 OS 资源且分配更慢，所以只对频繁搬运的优化器状态开，不适合无脑全开。

**练习 2**：除了 `device: "cpu"`，DeepSpeed 还支持把优化器状态 offload 到哪里？为什么本项目选 CPU？

**参考答案**：还可 offload 到 NVMe（`device: "nvme"`）以利用更大且更便宜的 SSD 存储。本项目选 CPU 是因为 6.7B 的优化器状态分片（~20 GB/卡）总量在普通工作站 CPU 内存（数百 GB）范围内，CPU 已足够且延迟远低于 NVMe。

---

### 4.3 fp16 动态 loss scale：混合精度的稳定性保障

#### 4.3.1 概念说明

训练用 fp16 是为了**省一半显存、 doubling 算力**（Tensor Core 对 fp16 加速明显）。但 fp16 的致命弱点是**动态范围极窄**：最大约 65504，最小的正规数约 6.1×10⁻⁵——比这更小的梯度会「下溢（underflow）」成 0，对应参数就停止学习。深度网络的反向梯度常常落在 fp16 的下溢区。

**Loss scaling（损失缩放）** 是标准解法：反向前把 loss 乘一个大因子 \(S\)，梯度随之放大 \(S\) 倍从而脱离下溢区；优化器更新前再除以 \(S\) 还原。**动态** loss scale 会自动找合适的 \(S\)：出现溢出就调小，长期稳定就调大。这就是 [train/ds_stage_2.json:L2-L9](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/ds_stage_2.json#L2-L9) 这一整块的作用。

> 对照知识：bf16 的指数位与 fp32 相同（8 位），动态范围大，**不需要** loss scaling。本项目用 fp16，所以必须配这一块；若硬件支持且改用 `--bf16`，本块可省。

#### 4.3.2 核心流程

动态 loss scale 是一个带反馈的小状态机，\(S\) 的初值由 `initial_scale_power` 决定：

\[
S_0 = 2^{\text{initial\_scale\_power}} = 2^{16} = 65536
\]

每个训练步：

```text
loss_scaled = loss × S
loss_scaled.backward()              # 梯度被放大 S 倍
if 梯度里出现 inf/nan:              # 说明 S 太大，上溢了
    跳过本步 optimizer.step()
    S ← S / 2                        # 减半
    连续成功计数 ← 0
else:
    梯度 ← 梯度 / S 还原
    optimizer.step()
    连续成功计数 += 1
    if 连续成功计数 == loss_scale_window(1000):
        S ← S × 2                    # 翻倍，试探能否用更大动态范围
        连续成功计数 ← 0
若 S 被减到 < min_loss_scale(1)：报错/警告，梯度无法恢复。
```

`hysteresis: 2` 是一个「滞回」阻尼参数：它要求最近若干步都稳定才允许把 \(S\) 调回去，避免 \(S\) 在溢出边界附近来回震荡（刚减半又立刻翻倍）。

#### 4.3.3 源码精读

[train/ds_stage_2.json:L2-L9](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/ds_stage_2.json#L2-L9) 逐字段对照 4.3.2 的状态机：

| 字段 | 值 | 含义 |
| --- | --- | --- |
| `enabled` | `"auto"` | 由 HF Trainer 按命令行 `--fp16 True` 自动决定开启（见 README 命令里的 `--fp16 True`） |
| `loss_scale` | `0` | `0`/`"auto"` = **动态**缩放；填正数则表示固定静态缩放 |
| `loss_scale_window` | `1000` | 连续 1000 步无溢出才尝试把 \(S\) 翻倍 |
| `initial_scale_power` | `16` | 初始 \(S = 2^{16} = 65536\) |
| `hysteresis` | `2` | 滞回阻尼，防止 \(S\) 在边界震荡 |
| `min_loss_scale` | `1` | \(S\) 的下限（\(2^0=1\)）；跌破即认为梯度不可恢复 |

注意 `loss_scale: 0` 与 `enabled: "auto"` 是两个独立开关：前者控制「动 vs 静」，后者控制「开 vs 关」。两者配合的结果是「开启 fp16 + 动态缩放」。

#### 4.3.4 代码实践

**实践目标**：在纸上/代码里追踪这个状态机，理解每个参数如何影响 \(S\) 的演化（推理型实践，无需 GPU）。

**操作步骤**（示例代码，模拟状态机）：

```python
# 示例代码：模拟 DeepSpeed 动态 loss scale 状态机
S = 2 ** 16            # initial_scale_power=16 → 65536
window = 1000          # loss_scale_window
consec = 0
overflows = [False]*950 + [True] + [False]*49   # 第951步溢出，之后恢复

for step, overflow in enumerate(overflows):
    if overflow:
        S = max(S // 2, 1)
        consec = 0
        print(f"step {step}: 溢出 → S 减半为 {S}")
    else:
        consec += 1
        if consec == window:
            S *= 2
            consec = 0
            print(f"step {step}: 连续{window}步稳定 → S 翻倍为 {S}")
print("最终 S =", S)
```

**需要观察的现象**：第 951 步溢出时 \(S\) 从 65536 减半到 32768；之后需要连续 1000 步无溢出才会再次翻倍。

**预期结果**：上述 1000 步序列里，951 步减半后剩余步数不足 1000，故最终 \(S=32768\)。可见 `loss_scale_window` 越大，\(S\) 越保守、震荡越小但适应越慢。

> 待本地验证：真实训练中溢出是随机的，实际 \(S\) 轨迹取决于模型与学习率；可观察 DeepSpeed 日志里的 `loss scale` 行。

#### 4.3.5 小练习与答案

**练习 1**：把 `initial_scale_power` 设成 32 会怎样？

**参考答案**：初始 \(S=2^{32}\approx 4.3\times10^9\)，过大，几乎必然在第一步就上溢，随后被不断减半直到找到不溢出的值。设置过大不会出错（状态机会自动减半），但开头会浪费若干步。

**练习 2**：为什么 `min_loss_scale` 设为 1 而不是 0？

**参考答案**：\(S=1\)（\(2^0\)）已是不放大 loss 的极限。若 \(S=1\) 仍持续溢出，说明梯度本身就超出 fp16 范围、无法靠缩放挽救，此时应报警而非继续——`min_loss_scale` 是「放弃阈值」。

---

### 4.4 torchrun 多卡启动与「auto」配置的协同

#### 4.4.1 概念说明

配置文件里有一批字段写成 `"auto"`：`optimizer` 的 lr/betas/eps/weight_decay、`gradient_accumulation_steps`、`gradient_clipping`、`train_batch_size`、`train_micro_batch_size_per_gpu`。`"auto"` 的含义是「**不写死，让 HF Trainer 用命令行参数来填**」。这是为什么**同一份 `ds_stage_2.json` 能同时服务三种训练方案**——脚本之间只有命令行 batch 参数不同，配置本身完全共享。

`torchrun` 则是把这些命令行参数真正落到 N 个 GPU 进程上的启动器。

#### 4.4.2 核心流程

**启动侧（torchrun）**：

```text
torchrun --nproc_per_node=4 mle.py --deepspeed ds_stage_2.json --per_device_train_batch_size 2 ...
        │                  │                               │
        │                  └─ 脚本（4 份进程，每张 GPU 一份）
        └─ 起几个进程 = 用几张 GPU（单机多卡）
torchrun 自动注入环境变量：RANK / WORLD_SIZE / LOCAL_RANK / MASTER_ADDR / MASTER_PORT
```

**配置回填侧（HF Trainer + DeepSpeed）**：Trainer 读到 `--deepspeed ds_stage_2.json` 后，把 `"auto"` 字段按下表回填：

| 配置里的 `"auto"` 字段 | 回填来源（命令行参数） |
| --- | --- |
| `train_micro_batch_size_per_gpu` | `--per_device_train_batch_size` |
| `gradient_accumulation_steps` | `--gradient_accumulation_steps` |
| `train_batch_size` | 自动算 = micro × accum × world_size |
| `optimizer.params.lr` | `--learning_rate` |
| `optimizer.params.betas / eps / weight_decay` | 对应命令行（未给则用默认） |
| `fp16.enabled` | `--fp16 True` |

**有效 batch 对齐验证**（这是本讲的「金标准」不变量，world_size=4）：

| 脚本 | micro | accum | 有效 batch = 4 × micro × accum |
| --- | --- | --- | --- |
| `mle.py`（SFT） | 2 | 32 | **256** |
| `mle_scoring.py`（评分） | 1 | 64 | **256** |
| `mle_scoring_grad_split.py` | 1 | 64 | **256** |

三种方案的有效 batch 都精确对齐到 256——评分方案因候选数多、单卡只能开 micro=1，于是把累积步从 32 翻到 64 来补齐，保证三者学习率/收敛行为可比。

#### 4.4.3 源码精读

三条启动命令结构完全一致，差异只在两个 batch 参数。以标准 SFT 为例：

[README.md:L240-L260](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/README.md#L240-L260) —— 注意三个关键点：

- L240 `torchrun --nproc_per_node=4 mle.py`：起 4 进程。
- L246 `--per_device_train_batch_size 2` 与 L248 `--gradient_accumulation_steps 32`：回填进配置的两个 `"auto"` 字段。
- L257 `--gradient_checkpointing True` 与 L258 `--deepspeed ds_stage_2.json`。

评分方案见 [README.md:L265-L285](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/README.md#L265-L285)（L271 micro=1、L273 accum=64、L283 同一份 deepspeed 配置）；梯度切分方案见 [README.md:L289-L309](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/README.md#L289-L309)（L295 micro=1、L297 accum=64、L307 同一份配置）。

> 关于 `--gradient_checkpointing True`（README L257/L282/L306）：这是**命令行侧**的另一个省显存手段，不在 `ds_stage_2.json` 里。它通过「反向时重算激活」来砍激活值显存，代价是多一次前向计算。它与 ZeRO（砍训练状态显存）正交：ZeRO 省的是参数/梯度/优化器，gradient checkpointing 省的是激活值，两者叠加才能装下评分训练的大 batch。

#### 4.4.4 代码实践

**实践目标**：验证三条命令的有效 batch 确实都等于 256，并亲手把命令行参数映射到配置的 `"auto"` 字段（纯计算，无需 GPU）。

**操作步骤**（示例代码）：

```python
# 示例代码：有效 batch 校验 + auto 字段映射
world = 4
cmds = {
    "mle.py(SFT)":           dict(micro=2, accum=32),
    "mle_scoring.py":        dict(micro=1, accum=64),
    "mle_scoring_grad_split":dict(micro=1, accum=64),
}
for name, p in cmds.items():
    eff = world * p["micro"] * p["accum"]
    print(f"{name:28s} micro={p['micro']} accum={p['accum']} → 有效batch={eff}  {'✓对齐256' if eff==256 else '✗'}")

# 映射：命令行 --per_device_train_batch_size 2 会回填到哪个配置字段？
auto_map = {
    "--per_device_train_batch_size": "train_micro_batch_size_per_gpu",
    "--gradient_accumulation_steps": "gradient_accumulation_steps",
    "--learning_rate":               "optimizer.params.lr",
    "--fp16 True":                   "fp16.enabled",
}
for cli, cfg in auto_map.items():
    print(f"{cli:38s} → ds_stage_2.json: {cfg}")
```

**需要观察的现象**：三条命令有效 batch 均为 256；命令行的 micro/accum 正好对应配置里那两个 `"auto"` 字段。

**预期结果**：全部打印 `✓对齐256`，证明评分方案是用累积步补齐 micro 的下降、而非改变学习规模。

#### 4.4.5 小练习与答案

**练习 1**：如果只有 2 张 GPU，想保持有效 batch=256，`mle.py` 的 `gradient_accumulation_steps` 应改成多少？

**参考答案**：有效 batch = world × micro × accum = 2 × 2 × accum = 256 → accum = 64。这正是「换卡数就调累积步」的典型操作，而配置文件因为用了 `"auto"` 完全不用改。

**练习 2**：为什么要把 batch 参数写成 `"auto"` 而不是直接写死在 `ds_stage_2.json` 里？

**参考答案**：写死会导致一份配置只能服务一种脚本/一种卡数；用 `"auto"` 让 batch/累积步/学习率由命令行决定，于是同一份 `ds_stage_2.json` 同时服务 SFT、评分、梯度切分三种方案，也方便换机器时只改命令行、不动配置。

---

## 5. 综合实践

把本讲四块知识串成一个完整任务：**关掉 optimizer offload，预测显存与速度，并论证为什么评分训练比普通 SFT 更需要 offload。**

### 任务

1. **改配置**（不要动原文件）：复制 `train/ds_stage_2.json` 为 `train/ds_stage_2_no_offload.json`，删除 [train/ds_stage_2.json:L24-L27](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/ds_stage_2.json#L24-L27) 的 `offload_optimizer` 段，确保 JSON 合法。
2. **算显存**：对 6.7B、4 卡，分别估算开/关 offload 时每卡 GPU 训练状态显存（不含激活），写出 GPU 侧从 \(2\Psi + 2\Psi/N_d\) 涨回 \(2\Psi + 14\Psi/N_d\) 的差值（约 +20 GB）。
3. **预测**：填一张表，对 `mle.py` 与 `mle_scoring.py` 各预测「关掉 offload 后能否跑通、显存与速度方向」。
4. **论证**：解释为什么评分训练更依赖 offload。

### 论证要点（参考答案）

普通 SFT（`mle.py`）每个样本是**一条** query+response 序列，micro=2 时前向只过 2 条序列，激活值正比于 `2 × model_max_length`。

评分训练（`mle_scoring.py`）每条指令经 `DataCollator` 展开成 **N 个候选**（见 u3-l1），micro=1 实际前向的是 N 条序列；且 `compute_loss` 要把 logits 重排成 \((B, N, L, V)\)，\(V\) 是词表（~32000），这个 logits 张量极大。所以同样「batch=1」，评分训练的激活值与 logits 占用是 SFT 的约 N 倍。

结论链：

\[
\text{评分训练激活}\uparrow N\text{倍} \;\Rightarrow\; \text{GPU 显存极度紧张} \;\Rightarrow\; \begin{cases}\text{ZeRO-2 + offload 把训练状态搬走（本讲，系统侧）}\\ \text{gradient\_checkpointing 砍激活（命令行侧）}\\ \text{梯度切分逐候选前向（u3-l3，算法侧）}\end{cases}
\]

三者从不同角度省显存，共同目标都是让评分训练在 micro=1 下不 OOM。关掉 offload 会把 ~20 GB 还回 GPU，最先倒下的就是评分训练——所以**评分训练比普通 SFT 更需要 offload**。这也解释了为何 README 在评分方案下特意提示「If your gpu could't afford batch size 1 ... try the gradients splitting method」（[README.md:L287](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/README.md#L287)）：offload 不够时，还得叠算法侧的梯度切分。

> 待本地验证：显存预测需在真实 4 卡环境用 `nvidia-smi` 或 PyTorch `torch.cuda.max_memory_allocated()` 实测；可比较开/关 offload 两种配置的峰值显存与单步耗时。

## 6. 本讲小结

- ZeRO Stage 2 切分**优化器状态 + 梯度**、保留完整**参数**，是「省显存」与「少通信」的折中，每卡训练状态 \(M=2\Psi+14\Psi/N_d\)。
- `offload_optimizer: device=cpu` 把最胖的优化器状态分片搬到 CPU，GPU 侧再降到约 \(2\Psi+2\Psi/N_d\)；`pin_memory: true` 用页锁定内存加速 CPU↔GPU 搬运。
- fp16 动态 loss scale 用 \(S_0=2^{16}=65536\)、溢出减半、千步稳定翻倍的状态机，避免 fp16 梯度下溢；`loss_scale:0` = 动态，`enabled:"auto"` 由 `--fp16` 决定。
- `torchrun --nproc_per_node=4` 起多进程；配置里的 `"auto"` 字段由命令行回填，使一份 `ds_stage_2.json` 服务全部三种训练方案。
- 三种方案有效 batch 都对齐到 **256**（4×2×32 与 4×1×64），评分方案用更大的累积步补偿更小的 micro batch。
- 评分训练因每样本展开成 N 个候选、激活与 \((B,N,L,V)\) logits 膨胀，比 SFT 更依赖 offload，并与 `gradient_checkpointing`、u3-l3 梯度切分共同省显存。

## 7. 下一步学习建议

本讲给出的是**系统侧**省显存答案。建议接下来：

1. **对照算法侧方案**：阅读 u3-l3（`mle_scoring_grad_split.py`），看它如何通过覆写 `training_step` 逐候选前向，把单步显存峰值从正比于候选数 C 降到与 C 无关——与本讲的 offload 形成「算法省显存 vs 系统省显存」的完整图景。
2. **追溯配置消费方**：回到 u2-l7（`mle.py`）和 u3-l1（`mle_scoring.py`），确认它们都继承 HF `TrainingArguments`，由 Trainer 把命令行参数注入本讲的 `"auto"` 字段，把「配置—脚本—命令行」三者的契约闭环。
3. **进阶实验**（需多卡环境）：实测开/关 offload、开/关 `gradient_checkpointing`、改 ZeRO stage 的显存与吞吐曲线，把本讲的估算公式落到真实数字上。
