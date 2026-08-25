# BitDecoding 是什么：低比特 KV cache 解码加速总览

## 1. 本讲目标

读完本讲，你应该能够：

1. 用一句话说清 BitDecoding 的定位：它是一个 **GPU kernel 系统**，输入「2/4-bit 量化打包的 KV cache + 单 token 的 Query」，输出「注意力结果」，专门加速长上下文 LLM 的 decoding 阶段。
2. 解释**为什么长上下文 decoding 被 KV cache 的访存带宽卡住**，并能动手算出一份 KV cache 到底占多少显存。
3. 认识后续单元会反复出现的关键词：**2/4-bit、k-channel / k-tensor、group_size、residual、LOP3、split-KV**，知道它们分别在哪一讲被深入拆解。
4. 读懂 README 中的安装与快速开始入口，并从 4090 / A100 两张性能图中读出「3-9x 加速」的出处与适用条件。

本讲不要求你懂 CUDA，也不要求你已经编译过项目——所有推导都可以在纸面上完成。

## 2. 前置知识

### 2.1 LLM 的两个阶段：prefill 与 decode

大语言模型生成文本时分两步走：

- **prefill（预填充）**：把用户输入的整段 prompt 一次性喂进模型，并行计算所有 token 的注意力。计算量巨大，GPU 算力是瓶颈（compute-bound）。
- **decode（解码）**：之后每生成一个 token，都要拿**当前这 1 个 token 的 Query**，去和**历史上所有 token 的 Key/Value** 做注意力。这一步的计算量很小，但每一步都要把整段历史重新读一遍——显存带宽是瓶颈（memory-bound）。

### 2.2 什么是 KV cache

自回归生成时，历史 token 的 Key 和 Value 不会变，于是把它们缓存下来避免重算，这份缓存就是 **KV cache**。它随着上下文变长而**线性膨胀**：上下文 10 万 token 时，KV cache 可以达到十几 GB。decode 阶段每生成一个 token，GPU 都要把这十几 GB 从显存（HBM）搬进计算单元一次。

### 2.3 什么是量化（quantization）

把 FP16（16 位浮点）的数值压成更少的位数存储，例如 **4-bit**（16 档）或 **2-bit**（4 档）。常见做法是每组 `group_size` 个连续元素共用一组 `scale`（缩放）和 `zero`（零点）参数，量化与反量化公式为：

\[ q = \mathrm{round}\!\left(\frac{x - z}{s}\right), \qquad \hat{x} = s \cdot q + z \]

其中 \(x\) 是原始值，\(q\) 是量化后的整数，\(\hat{x}\) 是还原值。压缩的直接收益是**要搬运的字节变少了**：FP16 → int4 缩小 4 倍，FP16 → int2 缩小 8 倍。

### 2.4 什么是 Tensor Core

Tensor Core 是 NVIDIA GPU 上做矩阵乘法的专用硬件单元，一次指令可以完成一小块矩阵乘加。BitDecoding 的核心命题之一就是：**让低比特数据不用先在显存里还原成 FP16，而是直接在寄存器里"拼"成 Tensor Core 能吃的 fragment**。这个技巧（LOP3 反量化）将在第 5 单元拆解，本讲只需记住这个名字。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [README.md](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/README.md) | 项目定位、3-9x 加速声明、安装方式、GSM8K 快速开始、HPCA 2026 论文引用 |
| [imgs/overview.png](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/imgs/overview.png) | 总览图：BitDecoding 在整体解码流程中的位置 |
| [imgs/scheme.png](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/imgs/scheme.png) | 方案原理图：量化打包 → 解码 kernel → 输出的工作流 |
| [imgs/4090.png](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/imgs/4090.png) | RTX 4090 上的 kernel 性能对比 |
| [imgs/a100.png](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/imgs/a100.png) | A100 上的 kernel 性能对比 |
| [bit_decode/__init__.py](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/__init__.py) | Python 包的门面：版本号与对外暴露的全部符号 |
| [bit_decode/bit_decode_interface.py](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/bit_decode_interface.py) | 两个核心 API 的 Python 签名（本讲只看输入输出，第 2 单元精读） |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **项目定位与带宽瓶颈**：BitDecoding 解决什么问题，为什么低比特能加速 decoding。
2. **四张图**：overview、scheme 原理图与 4090 / A100 性能图。
3. **Python 包门面**：`bit_decode/__init__.py` 暴露的两个核心 API 及其输入输出。

### 4.1 项目定位：长上下文 decoding 的 KV cache 带宽瓶颈

#### 4.1.1 概念说明

BitDecoding 的自我定位写在 README 第一段：一个**高性能、GPU 优化的系统**，用**低比特 KV cache** 加速**长上下文 LLM 的 decoding**，相对 Flash Attention v2 取得 **3-9x 加速**。

理解它的关键，是理解 decode 阶段的一个残酷事实：**每生成一个 token，注意力计算本身只需要做两次小矩阵乘，但要把整个 KV cache 从显存读一遍**。我们算一笔账（示例推导，非仓库代码）：

单步 decode 中，对每个 (batch, head)：

- 计算量：\(QK^\top\) 约为 \(s \cdot d\) 次乘加，\(PV\) 约为 \(s \cdot d\) 次乘加，合计约 \(4sd\) FLOPs（\(s\) 为上下文长度，\(d\) 为 head 维度）；
- 读数据：K 与 V 共 \(2sd\) 个 FP16 元素 = \(4sd\) 字节。

于是算术强度（每读 1 字节能做多少次运算）约为：

\[ \frac{4sd \text{ FLOPs}}{4sd \text{ Bytes}} = 1 \ \text{FLOP/Byte} \]

而现代 GPU 的"峰值算力 / 峰值带宽"比值通常在 **100 FLOP/Byte 以上**（量级估计）。比值 1 意味着计算单元几乎全程在等数据——这就是 **memory-bound**。此时最有效的优化不是把乘法做得更快，而是**把要读的字节变少**：int4 让 KV 读取量降为 1/4，int2 降为 1/8。BitDecoding 的 3-9x 加速正是主要来自这份带宽节省（前提是：反量化足够便宜、精度损失可接受——这正是它用一整套 kernel 设计去保证的）。

#### 4.1.2 核心流程

从系统视角，BitDecoding 的工作流可以概括为：

```text
prefill 阶段
  FP16 的 K/V ──(QPack 量化打包 kernel)──> k_pack / k_params / v_pack / v_params
                                              （低比特主缓存，长期驻留）

decode 阶段（每生成一个 token 一次）
  单 token Query ─┐
  低比特 KV cache ─┼─(split-KV 解码 kernel + LOP3 反量化 + Tensor Core)──> 注意力输出 out
  FP16 残余/新 kv ─┘        （攒满一个块后在 kernel 内原位再量化，拼回主缓存）
```

三个关键设计（后续单元逐一展开）：

- **打包（pack）**：多个低比特整数压进一个 `uint16` 容器（`pack_num = 16 / num_bits`，即 4-bit 压 4 个、2-bit 压 8 个）。
- **残余（residual）**：最新的、不足一个块的 token 保留 FP16 精度参与计算，攒满一块再量化，兼顾精度与效率。
- **split-KV**：decoding 时 batch × heads 很小、GPU 大量算力闲置，把长 KV 切成多份并行算，最后用 log-sum-exp 合并。

#### 4.1.3 源码精读

README 开头就是全项目最重要的一句话：

> [README.md:L5-L7](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/README.md#L5-L7) — 项目定位：用低比特 KV cache 加速长上下文 LLM decoding 的高性能 GPU 优化系统，相对 Flash Attention v2 达到 3-9x 加速。这两个数字（低比特、3-9x）是贯穿整个学习手册的主线索。

论文出处（HPCA 2026）也在 README 中给出，标题一句话点破了机制——"Unlocking Tensor Cores for Long-Context LLMs with Low-Bit KV Cache"（用低比特 KV cache 解锁 Tensor Core）：

> [README.md:L38-L47](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/README.md#L38-L47) — BibTeX 引用：Du, Dayou 等，HPCA 2026。注意标题里的 "Unlocking Tensor Cores"：加速不只来自"读得少"，还来自"反量化后的数据能直接进 Tensor Core"。

安装与快速开始入口（本讲只认门，第 2 讲 `u1-l2` 才走完整编译流程）：

> [README.md:L17-L24](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/README.md#L17-L24) — 安装四步：`git clone --recursive`（拉取 cutlass 子模块）→ 建 conda 环境 → `pip install -r requirements.txt` → `bash install.sh` 编译 CUDA 扩展。

> [README.md:L26-L31](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/README.md#L26-L31) — 快速开始：进入 `evaluation` 目录运行 `bash scripts/example.sh`，跑 GSM8K 长上下文生成示例。

两个值得注意的细节：

- README 的 clone 地址写的是 `DD-DuDa/BitDecoding`（[README.md:L19](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/README.md#L19)），而本手册分析的仓库是 `OpenBitSys/BitDecoding`——后者是同一项目的组织仓库，阅读源码时以本仓库 HEAD 为准。
- [README.md:L50-L51](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/README.md#L50-L51) 的致谢列表透露了技术血统：本项目改造自 **flash-attention**（kernel 骨架）、参考了 **KIVI**（低比特 KV cache 的先驱）与 **flute**（LOP3 式反量化的同类思路）等。知道血统，后面读代码时就不会把 FlashAttention 的遗留结构误认为是 BitDecoding 的原创。

#### 4.1.4 代码实践：写一个 KV cache 显存计算器

**实践目标**：用一杯纸面推导 + 一段纯 Python 代码，亲眼看到"低比特把 KV cache 缩小多少倍"，为 3-9x 加速建立数量级直觉。

**操作步骤**（不需要 GPU，任何有 Python 的机器均可）：

1. 新建 `kv_calc.py`，写入下面的**示例代码**（非仓库代码）：

```python
def kv_cache_bytes(layers, kv_heads, head_dim, seq_len, batch=1, bits=16):
    """按给定配置估算 KV cache 字节数。"""
    elements = 2 * layers * kv_heads * head_dim * seq_len * batch  # 2 = K 和 V
    return elements * bits // 8

# Llama-3.1-8B: 32 层, 8 个 KV 头 (GQA), head_dim=128
cfg = dict(layers=32, kv_heads=8, head_dim=128)
for s in (1_000, 10_000, 100_000):
    fp16 = kv_cache_bytes(seq_len=s, bits=16, **cfg)
    i4   = kv_cache_bytes(seq_len=s, bits=4,  **cfg)
    i2   = kv_cache_bytes(seq_len=s, bits=2,  **cfg)
    print(f"ctx={s:>7}: FP16={fp16/1e9:6.2f} GB, int4={i4/1e9:6.2f} GB, int2={i2/1e9:6.2f} GB")
```

2. 运行 `python kv_calc.py`。
3. 换一组参数再跑一次：把 `kv_heads` 改成 32（无 GQA 的模型），观察差异。

**需要观察的现象**：上下文每放大 10 倍，三种精度的显存都放大 10 倍；int4 恰为 FP16 的 1/4，int2 为 1/8。

**预期结果**：ctx=100,000 时约得到 FP16 ≈ 13.1 GB、int4 ≈ 3.3 GB、int2 ≈ 1.6 GB（按 1 GB = 10⁹ 字节计）。也就是说，A100 80GB 上单靠 FP16 cache 就能吃掉约 1/6 显存，而 decode 每步都要把它完整读一遍——这就是低比特的直接动机。（具体数值待本地验证，取决于你的参数代入。）

#### 4.1.5 小练习与答案

**练习 1**：一台 GPU 的 FP16 Tensor Core 峰值算力约为 300 TFLOPS、HBM 带宽约为 2 TB/s。decode 注意力的算术强度约为 1 FLOP/Byte，问：要让计算单元满负荷，需要多少？

**答案**：带宽折算的"喂饱算力"要求为 300×10¹² FLOPs/s ÷ 2×10⁹ Bytes/s = 150 FLOP/Byte；而 decode 只有约 1 FLOP/Byte，相差两个数量级——计算单元利用率理论上限只有约 1/150，所以优化方向是减字节而不是加算力。

**练习 2**：为什么同样的低比特 KV cache，对 prefill 加速有限，对 decode 加速显著？

**答案**：prefill 有大量 Query 并行，同一段 KV 会被复用很多次，属于 compute-bound，减字节收益小；decode 每步只有 1 个 Query，KV 读一遍只用一次，属于 memory-bound，读取字节数近乎线性地决定耗时，int4/int2 直接把这项开销降为 1/4、1/8。

**练习 3**：BitDecoding 声称的 3-9x 中，"9x" 一端更可能出现在哪种配置下？

**答案**：低比特（2-bit）+ 上下文很长 + batch/头数小（GPU 更闲、瓶颈更纯粹在带宽）的组合；此时节省的读取字节比例最大。可结合 4.2 节的性能图验证这一推断。

### 4.2 四张图：方案原理与 4090 / A100 性能证据

#### 4.2.1 概念说明

README 内嵌了四张图：overview 与 scheme 讲"是什么、怎么做"，4090 与 a100 讲"有多快"。读图是本讲建立全局认知最快的方式——先把图里的名词混个脸熟，后面单元再逐个击破。

#### 4.2.2 核心流程

四张图的阅读顺序建议：

```text
overview.png（总览） ──> scheme.png（原理） ──> 4090.png / a100.png（证据）
   它在哪条链路上        它内部怎么干活          它到底快多少、何时快
```

读性能图的通用方法：

1. 先看横轴与纵轴的物理含义（长度、加速比/耗时）；
2. 再看图例里有几条曲线、分别对应什么位宽/方法；
3. 最后看趋势：加速比随上下文长度如何变化、在哪个区间最陡、是否趋于平稳。

#### 4.2.3 图表精读

overview 与 scheme 两张图由 README 直接内嵌在项目定位之后：

> [README.md:L8-L9](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/README.md#L8-L9) — 在定位声明下方连续嵌入 overview 与 scheme 两张图。overview 图给出 BitDecoding 相对整体解码流程的位置关系；scheme 图以框图形式展开工作流：量化打包后的 KV cache、decode kernel 内部的处理阶段、以及 FP16 残余/新 token 如何汇入。建议在本地打开大图逐块辨认（图片同时在仓库 [imgs/overview.png](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/imgs/overview.png) 与 [imgs/scheme.png](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/imgs/scheme.png)，本讲义目录下也可用 `../imgs/overview.png` 相对路径查看）：

![overview](../imgs/overview.png)

![scheme](../imgs/scheme.png)

性能图在 Benchmark 小节：

> [README.md:L11-L15](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/README.md#L11-L15) — 标注为 "Kernel Performance in RTX4090" 与 "Kernel Performance in A100"，对应 [imgs/4090.png](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/imgs/4090.png) 与 [imgs/a100.png](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/imgs/a100.png)：

![4090](../imgs/4090.png)

![a100](../imgs/a100.png)

两图均为 kernel 级对比（不是端到端生成速度）：横轴为 KV 序列长度，纵轴为相对基线的性能倍数，曲线按位宽/方法区分（以图中坐标轴与图例标注为准）。它们是 README "3-9x" 声称的直接出处：上限来自低比特 + 长上下文的最优配置，下限对应较短上下文或较高精度配置。

#### 4.2.4 代码实践：从性能图中提取三个事实

**实践目标**：把"3-9x"从口号变成你亲手读出来的数字，并验证 4.1 节的推断——上下文越长、位宽越低，收益越大。

**操作步骤**：

1. 在本地打开 `imgs/4090.png` 与 `imgs/a100.png`（或用上面嵌入的图）。
2. 对每张图记录：横轴的取值范围、图例中每条曲线的名字。
3. 任选一条低比特曲线，读出它在最短与最长上下文处的纵坐标（近似值即可）。
4. 对比同一上下文长度下 int2 与 int4 曲线（若图中都有）的高低关系。
5. 对比同一配置在 4090 与 A100 两图中的加速倍数差异。

**需要观察的现象**：曲线是否随横轴增大而上升后趋于平稳；2-bit 曲线是否整体高于 4-bit 曲线；两张 GPU 图的趋势是否一致。

**预期结果**：低比特曲线在长上下文端达到最大加速（与 README 的 3-9x 上限吻合），短上下文端收益明显收窄；两张图趋势一致但幅度不同（不同 GPU 的带宽/算力比不同）。具体读数由你在图上完成——本手册不代抄数字，请把你读到的数值记录下来（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：scheme 图中为什么要专门画出一块 FP16 的"残余/新 token"通路，而不是把所有 KV 一律量化？

**答案**：最近的 token 对输出影响大，且数量不足一个量化块（residual_block_size）时参数估计不准；保留 FP16 精确计算、攒满一块再量化，是用很小的显存代价换取精度保护（细节在第 2 单元 `u2-l2`）。

**练习 2**：性能图纵轴若改为"绝对耗时"而不是"加速比"，曲线形状会怎么变？

**答案**：所有方法的耗时都会随上下文近似线性上升；加速比图相当于两条耗时曲线相除，低比特方法的斜率更小，所以商随长度增大——这正是"上下文越长收益越大"的图像化解释。

**练习 3**：4090 与 A100 的 HBM 带宽相差不大（量级上同属 1-2 TB/s），但算力特性不同。为什么同一 kernel 在两张卡上的加速倍数会不一样？

**答案**：加速比 = 基线耗时 / 优化后耗时，两张卡上基线（FlashAttention v2）与优化 kernel 的带宽利用、occupancy、split 策略表现都不同；decode 是带宽受限，卡与卡之间"带宽/算力比"不同会放大或缩小低比特收益。这也是第 3 单元 split 启发式要按 GPU 属性调参的原因。

### 4.3 Python 包门面：`bit_decode/__init__.py` 暴露了什么

#### 4.3.1 概念说明

无论内部 kernel 多复杂，外部使用者能看到的 BitDecoding 只有薄薄一层 Python 包。`bit_decode/__init__.py` 就是这层门面：它声明版本号，并只导出**两个核心函数**（量化打包、低比特解码）和**三个缓存类**。本模块的目标是记住这两个函数的"输入 → 输出"契约——它就是整本手册要拆开来看的黑盒。

#### 4.3.2 核心流程

两个 API 在系统中的角色：

```text
kvcache_pack_int（prefill 时调用一次 + 残余攒满时内核内部再调用）
    输入: FP16 的 k_cache / v_cache + 预分配的 k_pack/k_params/v_pack/v_params
    输出: 原地写入低比特打包缓存（无返回值）

fwd_kvcache_int（decode 每步调用一次）
    输入: q（单 token Query）
          k_pack/k_params/v_pack/v_params（低比特主缓存）
          opt_k_new/opt_v_new（FP16 新 token，可选）
    输出: out_bit（注意力输出）
          k_pack_new/k_params_new/v_pack_new/v_params_new（攒满一块时新量化的缓存）
```

#### 4.3.3 源码精读

整个门面只有 8 行：

> [bit_decode/__init__.py:L1-L6](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/__init__.py#L1-L6) — 声明版本 `1.0.0.post1`，并从 `bit_decode_interface` 导入两个核心函数 `kvcache_pack_int` 与 `fwd_kvcache_int`。这两行就是全项目 Python 侧的全部功能入口。

> [bit_decode/__init__.py:L8](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/__init__.py#L8) — 从 `bit_decode.models.cache_utils` 导入 `Cache / DynamicCache / StaticCache` 三个缓存类。这是改造版 HuggingFace 缓存（第 6 单元 `u6-l1` 的主角），本讲只需知道"低比特缓存以缓存类的形式挂在模型上"。

两个函数的真实签名（第 2 单元 `u2-l3` 逐参数精读，这里只看骨架）：

> [bit_decode/bit_decode_interface.py:L12-L19](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/bit_decode_interface.py#L12-L19) — `kvcache_pack_int` 的参数表：FP16 的 `k_cache/v_cache`，预分配的打包张量 `k_pack/k_params/v_pack/v_params`，以及三个本讲的关键词参数——`quant_mode: str = "k-tensor"`、`group_size: int = 128`、`num_bits: int = 4`。**k-channel / k-tensor 两种量化模式与 group_size 在接口默认值里就已经出现**。

> [bit_decode/bit_decode_interface.py:L47-L61](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/bit_decode_interface.py#L47-L61) — `fwd_kvcache_int` 的参数表：查询 `q`、低比特缓存四件套、FP16 的新 kv（`opt_k_new/opt_v_new`）、接收新量化结果的四个 `*_new` 张量，以及 `residual_block_size: int = 128` 等配置。输入输出契约与 4.3.2 的图示一一对应。

> [bit_decode/bit_decode_interface.py:L107](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/bit_decode_interface.py#L107) — 返回值共 5 个：`out_bit` 加上 4 个 `*_new` 张量。

`num_bits` 在这一层完成分流：

> [bit_decode/bit_decode_interface.py:L26-L45](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/bit_decode_interface.py#L26-L45) — `num_bits == 4` 调 `bit_decode_cuda.kvcache_pack_int4`，`num_bits == 2` 调 `kvcache_pack_int2`，其余值抛出 `ValueError`。`bit_decode_cuda` 是第 3 单元将拆解的 pybind11 扩展——从这行 import 就能看出 Python 层完全不碰 CUDA 细节。

由此可以把本讲的关键词表整理如下（"深入讲次"均为后续单元）：

| 关键词 | 一句话含义 | 深入讲次 |
| --- | --- | --- |
| 2/4-bit | KV cache 每元素压缩到 2 或 4 比特，读取字节降为 FP16 的 1/8 或 1/4 | u4-l3、u5-l3 |
| k-channel / k-tensor | K 的两种量化粒度：逐通道独立 scale/zero，或整张量按组共用 | u2-l1 |
| group_size | 每 g 个连续元素共享一组 scale/zero 参数 | u2-l1 |
| residual（残余） | 最新不足一块的 token 保留 FP16，攒满 residual_block_size 再量化 | u2-l2、u5-l4 |
| LOP3 | 用位操作指令在寄存器内把低比特整数重排成 FP16 fragment | u5-l3 |
| split-KV | 把长 KV 切成多份并行计算再合并，提高 decoding 时的 SM 占用 | u3-l3、u5-l5 |

#### 4.3.4 代码实践：盘点包的对外表面

**实践目标**：不运行任何 GPU 代码，仅凭源码列出 `bit_decode` 包对外暴露的全部符号，并核对"两个函数 + 三个类"的说法。

**操作步骤**：

1. 打开 [bit_decode/__init__.py](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/__init__.py)（全文 8 行），抄下所有被 import 的名字。
2. 用 grep 确认没有遗漏的导出：在仓库根目录执行 `grep -rn "from bit_decode import" evaluation/ bit_decode/`，观察模型代码实际从包里取用了哪些符号。
3. （可选，需先完成第 2 讲的编译安装）运行 `python -c "import bit_decode; print(bit_decode.__version__); print(dir(bit_decode))"`。

**需要观察的现象**：第 2 步应能看到 `evaluation/llama.py`、`evaluation/qwen3.py` 等模型文件从 `bit_decode` 取用函数与缓存类；第 3 步（若可运行）应打印 `1.0.0.post1` 与含 `kvcache_pack_int`、`fwd_kvcache_int`、`DynamicCache` 的符号列表。

**预期结果**：对外表面恰好是 `__version__` + 2 个函数 + 3 个缓存类；这验证了"外部只需两个 API"的分层设计。第 3 步依赖 `bit_decode_cuda` 编译成功（`bit_decode_interface.py` 第 10 行 import 它），未编译时会报 `ModuleNotFoundError`——属预期现象，完成 `u1-l2` 后即可运行（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`kvcache_pack_int` 与 `fwd_kvcache_int` 分别在哪个阶段被调用？

**答案**：`kvcache_pack_int` 在 prefill 结束后把 FP16 的 K/V 量化打包进主缓存（残余攒满时由解码 kernel 在 GPU 侧完成同等操作）；`fwd_kvcache_int` 在 decode 的每一步被调用，输入单 token Query 与缓存，输出注意力结果。

**练习 2**：为什么 `fwd_kvcache_int` 要把 4 个 `*_new` 张量既作为输入又作为输出？

**答案**：它们是预分配的输出缓冲：当 FP16 残余攒满一个 `residual_block_size` 时，kernel 顺带把这块量化写入这 4 个张量返回给 Python 侧，再由缓存类拼回主缓存；作为入参传入可以复用缓冲、避免每步重新分配显存（完整生命周期见 `u5-l4`）。

**练习 3**：接口默认 `quant_mode="k-tensor"`，而本手册大纲说 K 支持 k-channel 与 k-tensor 两种模式。从本讲哪个证据可以确认模式是可选的？

**答案**：`bit_decode_interface.py` 中两个函数的 `quant_mode: str = "k-tensor"` 参数本身就是证据——它是字符串参数而非写死的常量，且 `kvcache_pack_int` 把它原样传给 CUDA 扩展（[bit_decode/bit_decode_interface.py:L17](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/bit_decode_interface.py#L17)）；两种模式张量形状的差异在 `u2-l1` 展开。

## 5. 综合实践

本讲的综合实践是规格中指定的读文献任务，产出一份「入门笔记」，后续单元会逐一回答你今天写下的疑问：

1. **通读 [README.md](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/README.md) 全文**（52 行，5 分钟）。
2. **细看两张 benchmark 图**：[imgs/4090.png](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/imgs/4090.png) 与 [imgs/a100.png](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/imgs/a100.png)，按 4.2.4 的方法读出坐标含义与曲线趋势。
3. **写一段不超过 5 句话的总结**，必须回答三件事：
   - BitDecoding 解决什么问题（长上下文 decode 的 KV cache 带宽瓶颈）；
   - 相对 Flash Attention v2 加速多少（README 声称 3-9x，结合你从图中读到的范围）；
   - 在什么条件下收益最大（位宽、上下文长度、batch 规模）。
4. **列出你尚不明白的 3 个术语**，填入下表并标注期望在哪一讲获得解答（可参考 4.3.3 的关键词表）：

| 术语 | 我目前的模糊理解 | 期望解答讲次 |
| --- | --- | --- |
| 例：group_size | 好像是多少个数共用一组 scale？ | u2-l1 |
| 术语 1 | … | … |
| 术语 2 | … | … |
| 术语 3 | … | … |

**验收标准**：总结里不出现"很快""很牛"这类空话，每个结论都能指回 README 的某一行或图的某个坐标；3 个术语是你真实没读懂的，而不是随手抄的。

## 6. 本讲小结

- BitDecoding 输入「2/4-bit 量化打包的 KV cache + 单 token Query」，输出「注意力结果」，是为长上下文 LLM **decode 阶段**设计的 GPU kernel 系统，相对 Flash Attention v2 加速 3-9x（HPCA 2026）。
- decode 是 memory-bound：算术强度约 1 FLOP/Byte，远低于 GPU 的算力/带宽比；低比特把 KV 读取字节降为 1/4（int4）或 1/8（int2），是加速的主要来源。
- 方案三要素：**量化打包**（uint16 容器按 `16/num_bits` 压包）、**FP16 残余区**（最新 token 保精度，攒满一块再量化）、**split-KV**（长 KV 切分并行 + LSE 合并）。
- Python 侧只有两个核心 API：`kvcache_pack_int`（打包）与 `fwd_kvcache_int`（解码），加上三个缓存类；`num_bits`、`quant_mode`（k-channel/k-tensor）、`group_size` 等关键词都出现在这两个函数的签名默认值里。
- README 是本讲的"源码"：定位声明、安装四步、GSM8K 快速开始与论文引用都在其中；clone 地址指向原作者仓库 `DD-DuDa/BitDecoding`，阅读以 `OpenBitSys/BitDecoding` 的 HEAD 为准。

## 7. 下一步学习建议

- 下一讲 **`u1-l2` 构建与安装**：按 README 的四步真正把 `bit_decode_cuda` 编译出来，本讲 4.3.4 实践中"可选的第 3 步"就能运行了。
- 想先看代码地图的读者可以跳到 **`u1-l3` 仓库结构**，了解 `bit_decode/`（Python）、`csrc/bit_decode/`（CUDA）、`evaluation/`（模型接入与测试）三层分工。
- 带着本讲综合实践写下的 3 个术语往下走：手册会在 `u2-l1`（量化布局）、`u2-l2`（residual）、`u5-l3`（LOP3）等讲次逐一回收这些问题。
- 延伸阅读（项目自己声明的血统）：FlashAttention 的 decoding 模式与 KIVI 的低比特 KV cache 论文，能帮助你在进入第 4、5 单元的 kernel 源码前区分"哪些结构继承自 FlashAttention、哪些是 BitDecoding 的改造"。
