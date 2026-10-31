# 长上下文 YaRN 与 FlashAttention 加速

## 1. 本讲目标

学完本讲，你应该能够：

1. **写出 1M 上下文的 `rope_scaling` 配置**：知道要在 `config.json` 里改哪几个字段、每个字段是什么含义。
2. **解释 Interleaved-MRoPE 下 factor 取 2~3 而非 4 的原因**：从位置 id 的增长机制层面说清楚，而不是死记结论。
3. **会启用 FlashAttention-2 加速推理**：包括安装、加载参数、硬件与精度限制。
4. 会组装 `vllm serve --rope-scaling` 的长上下文启动命令，并能粗估 KV cache 显存。

本讲是单元三（推理进阶与生产部署）的收尾：u3-l1 讲清了「size 预算 → 视觉 token 数 → input_ids 长度」这条因果链，本讲回答的问题是——**当 input_ids 长到超过模型原生的 256K 上下文时怎么办，以及注意力计算太慢太占显存时怎么办**。

## 2. 前置知识

用通俗语言补几个本讲需要的概念，已熟悉的读者可以跳过。

**RoPE（旋转位置编码）是什么？**
Transformer 本身不知道 token 的先后顺序，需要给每个位置附加信息。RoPE 的做法是：把每个 token 的查询/键向量中的一部分维度看成二维平面上的点，按 token 的位置编号 \( p \) 旋转一个角度。不同维度用不同的旋转速度（频率），就像钟表——秒针转得快、时针转得慢。低频维度（波长长）负责区分「远处」的位置关系，高频维度（波长短）负责「近处」的精细关系。

**上下文窗口与 `max_position_embeddings`**
模型预训练时位置编号只见过 0 到 `max_position_embeddings-1`。Qwen3-VL 原生是 262144（即 256K）。超出这个范围的位置编号，模型在训练时从未见过，直接外推通常会导致输出混乱。这就是「扩展到 1M」要解决的核心问题。

**KV cache 为什么吃显存**
自回归生成时，每生成一个新 token，都要和前面所有 token 做注意力计算。为了不重复计算，推理引擎会把历史 token 的 Key/Value 向量缓存下来。缓存量与序列长度成正比：**序列长度 × 层数 × KV 头数 × 头维度 × 精度字节数 × 2（K 和 V）**。序列从 256K 涨到 1M，KV cache 也涨 4 倍——这是长上下文的第一个代价。

**标准注意力的显存瓶颈**
朴素实现的注意力要先把 \( L \times L \) 的注意力分数矩阵完整写进显存。\( L = 100000 \) 时就是 10 万 × 10 万个分数，几十 GB 都打不住。FlashAttention 通过分块计算（tiling）根本不落盘这个大矩阵，把显存从 \( O(L^2) \) 降到近似 \( O(L) \)，且计算结果与标准注意力**数学等价**（不是近似）。长上下文和 FlashAttention 因此是一对天然搭档。

## 3. 本讲源码地图

本讲的「源码」主要集中在 README 的两个小节，辅以两个真实代码文件作为机制证据：

| 文件 | 关键位置 | 作用 |
| --- | --- | --- |
| `README.md` | L763-L795「Processing Long Texts」 | YaRN 的 `config.json` 配置、vLLM 启动参数、factor 取值的官方说明 |
| `README.md` | L740-L761「Flash-Attention 2 to speed up generation」 | FlashAttention-2 的安装、限制与启用方式 |
| `README.md` | L152-L158、L858 | Quickstart 中对 flash_attention_2 的推荐注释；vLLM 版本要求 |
| `web_demo_mm.py` | L34-L37、L95-L102 | `--flash-attn2` 命令行开关与模型加载分支（真实工程入口） |
| `qwen-vl-finetune/qwenvl/data/rope2d.py` | L5-L18、L76-L91 | `get_rope_index_3`：位置 id 的实际计算逻辑，是解释 factor 取值的关键证据（完整精读在 u5-l4） |

> 提示：本仓库不含模型结构代码（模型实现在 transformers ≥ 4.57.0 中，见 [README.md:L132-L135](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L132-L135)），所以 YaRN 配置本身是「改模型目录下的 config.json」这种配置型操作，而不是改仓库源码。

## 4. 核心概念与源码讲解

### 4.1 YaRN 原理与 1M 上下文配置

#### 4.1.1 概念说明

Qwen3-VL 原生支持 256K 上下文，官方宣称可扩展到 1M——「handles books and hours-long video」（见 [README.md:L32](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L32)）。扩展的手段是 **YaRN**（Yet another Rotation extensioN，[论文](https://arxiv.org/abs/2309.00071)），一种「长度外推」技术：不动预训练权重，只修改推理时位置编码的频率计算方式，让模型在没见过的更长位置上仍能正常工作。

理解 YaRN 需要先看两个「前任方案」的缺陷：

- **直接外推**：位置编号直接用 256K 以上的值。模型没见过这些频率组合，效果崩溃。
- **线性内插（PI）**：把位置编号除以 factor 压回原范围。可行，但所有频率被均匀压低，**高频维度（负责近距离精细关系）也被破坏**，短文本能力下降。
- **NTK-aware**：只压低频、保留高频。但它是「一刀切」的频率缩放。
- **YaRN = NTK-by-parts + 注意力温度校正**：按每个维度的**波长**分组处理，并额外修正长序列下注意力的发散。

#### 4.1.2 核心流程

RoPE 第 \( i \) 个旋转维度的频率与波长为：

\[
\theta_i = \text{base}^{-2i/d}, \qquad \lambda_i = \frac{2\pi}{\theta_i}
\]

其中 \( d \) 是旋转维度数，base 通常为 10000。YaRN 按波长把维度分成三组：

| 组 | 判断条件 | 处理方式 |
| --- | --- | --- |
| 外推组 | 波长远小于原上下文（\(\lambda_i \ll L_{\text{orig}}\)） | 频率不动——这些维度本来就在原范围内转了很多圈，位置变长不影响 |
| 内插组 | 波长大于扩展后上下文（\(\lambda_i > L_{\text{new}}\)） | 按比例内插——反正它们一个周期都走不完 |
| 过渡组 | 介于两者之间 | 用平滑函数（ramp）从外推渐变到内插 |

再加上注意力温度校正：注意力 logits 除以

\[
t = 1 + 0.1 \ln(f)
\]

（例如 \( f = 3 \) 时 \( t \approx 1.11 \)），缓解长序列下注意力过度集中。这就是 YaRN 的全部骨架——**训练零改动、只改推理侧的频率表**，这也是它能用一个 `config.json` 字段开关的原因。

启用流程（对 transformers 和 vLLM 通用）：

```text
下载模型 → 修改 config.json 两处：
  ① max_position_embeddings: 262144 → 1000000
  ② 新增/修改 rope_scaling 字段（yarn 类型 + factor + mrope 相关字段）
→ 重新加载模型，框架读到 rope_scaling 后自动切换到 YaRN 频率表
```

#### 4.1.3 源码精读

官方说明位于 README「Processing Long Texts」一节：

> 当前 `config.json` 设定的上下文长度上限是 256K；要处理超过 256K 的输入，使用 YaRN；在受支持的框架（目前是 transformers 和 vLLM）中，修改 `config.json` 的 `max_position_embeddings` 与 `rope_scaling` 即可启用。

对应源码：[README.md:L763-L768](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L763-L768) 说明了背景与操作入口。

完整的配置片段在 [README.md:L770-L787](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L770-L787)：

```json
{
    "max_position_embeddings": 1000000,
    ...,
    "rope_scaling": {
        "rope_type": "yarn",
        "mrope_section": [24, 20, 20],
        "mrope_interleaved": true,
        "factor": 3.0,
        "original_max_position_embeddings": 262144
    },
    ...
}
```

逐字段解读：

| 字段 | 值 | 含义 |
| --- | --- | --- |
| `max_position_embeddings` | 1000000 | 扩展后模型「自称」能处理的位置上限，需同步调大 |
| `rope_type` | `"yarn"` | 使用 YaRN 缩放策略（框架据此选择频率计算实现） |
| `original_max_position_embeddings` | 262144 | **预训练时的原生上限**，YaRN 以它为基准判断波长分组，此值不能乱改 |
| `factor` | 3.0 | 缩放因子，决定内插程度。取值原则见 4.2 |
| `mrope_section` | `[24, 20, 20]` | 多模态 RoPE 的维度切分：时间/高/宽三段各占多少旋转维度 |
| `mrope_interleaved` | `true` | 启用 Qwen3-VL 的交错式 MRoPE（三段维度交错排列而非连续分段） |

注意 `factor` 与两个上下文值的算术关系：\( 1000000 / 262144 \approx 3.81 \)。朴素线性内插会取 factor=4，但官方取 3.0——这不是笔误，原因在 4.2 详解。

#### 4.1.4 代码实践

**实践目标**：不依赖 GPU，完成一份「1M 上下文 config.json」的生成与校验，熟悉每个字段的落盘位置。

**操作步骤**（以下为示例代码，非仓库原有文件）：

1. 只下载模型的 `config.json`（约几 KB，无需拉全套权重）：

```python
# 示例代码：download_and_patch_yarn.py
import json, shutil
from huggingface_hub import hf_hub_download

repo_id = "Qwen/Qwen3-VL-2B-Instruct"
cfg_path = hf_hub_download(repo_id=repo_id, filename="config.json")
local_dir = "./qwen3vl-2b-1m"          # 目标模型目录（仅演示 config）
shutil.copy(cfg_path, f"{local_dir}/config.json.bak")   # 先备份
```

2. 写入 YaRN 三处修改：

```python
# 示例代码（续）
cfg = json.load(open(f"{local_dir}/config.json.bak"))

print("修改前:", cfg["max_position_embeddings"],
      cfg.get("rope_scaling", {}))     # 观察原始值，确认 rope_scaling 是否已存在

cfg["max_position_embeddings"] = 1000000
cfg["rope_scaling"] = {
    "rope_type": "yarn",
    "mrope_section": [24, 20, 20],
    "mrope_interleaved": True,
    "factor": 3.0,
    "original_max_position_embeddings": 262144,
}
json.dump(cfg, open(f"{local_dir}/config.json", "w"), indent=4, ensure_ascii=False)
```

3. 校验：

```python
# 示例代码（续）
new = json.load(open(f"{local_dir}/config.json"))
assert new["max_position_embeddings"] == 1000000
assert new["rope_scaling"]["rope_type"] == "yarn"
assert new["rope_scaling"]["mrope_interleaved"] is True
print("OK: 1M YaRN config 就绪")
```

**需要观察的现象**：
- 修改前的 `rope_scaling`（如果存在）里 `rope_type` 多半是 `default` 或没有 `factor`；
- `original_max_position_embeddings` 必须写 262144（原生值），而不是跟着改成 1000000。

**预期结果**：`config.json` 与备份文件 diff 只有三类变化（`max_position_embeddings`、新增/替换 `rope_scaling` 整块）。若你的模型 config 中本来就有 `mrope_section`，保持其原值即可，不要照抄本讲示例。

> 待本地验证：`hf_hub_download` 下载的是官方 2B 模型原始 config，其中 `max_position_embeddings` 的实际初始值以你下载到的文件为准（README 针对的系列原生值为 262144）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `original_max_position_embeddings` 不能改成 1000000？
**答案**：它告诉 YaRN「预训练时见过多少位置」，是波长分组的基准。改成 1000000 会让框架认为所有波长都在原范围内、无需内插，YaRN 实际失效，模型在超过 256K 的位置上直接外推崩溃。

**练习 2**：YaRN 和「线性内插（PI）」的本质区别是什么？
**答案**：PI 对全部频率维度统一除以 factor，破坏高频维度的近距离分辨能力；YaRN 按波长分组——高频维度保持原频率、低频维度内插、中间平滑过渡，并附加注意力温度校正，长短距离能力都保留。

**练习 3**：不改 `max_position_embeddings`，只加 `rope_scaling`，会怎样？
**答案**：位置上限仍由 `max_position_embeddings` 约束，框架/引擎在超过该长度的输入上会直接报错或截断；YaRN 的频率表虽然变了，但「入场券」没发。两者必须一起改。

### 4.2 Interleaved-MRoPE 与 scaling factor 为什么取 2~3

#### 4.2.1 概念说明

Qwen3-VL 的位置编码不是普通 RoPE，而是 **MRoPE（多模态旋转位置编码）+ 交错排列（Interleaved）**。README 对架构的官方描述是：Interleaved-MRoPE 通过稳健的位置嵌入，把**全频段**分配给时间、宽、高三个维度（[README.md:L50](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L50)）。

拆开理解：

- **MRoPE**：位置 id 不是一行，而是 3 行——\( t \)（时间）、\( h \)（高）、\( w \)（宽）。文本 token 三行同增；视觉 token 按各自网格坐标分别推进。旋转维度按 `mrope_section: [24, 20, 20]` 切成三段，分别听 t/h/w 的指挥（24+20+20=64 个旋转维度，对应 head_dim 128 的设定；以你手上 config 的原值为准）。
- **Interleaved（交错）**：老款 MRoPE（Qwen2-VL/2.5-VL）把 64 维连续切成 [24 | 20 | 20] 三段，导致 t 段只占用其中一段的频率范围；Qwen3-VL 改为交错排列（t,h,w,t,h,w,…），三个维度都能用上从高频到低频的**全频段**——这就是「Full-frequency allocation」的含义。
- **时间戳替代 t 维增长**：Qwen3-VL 用「文本-时间戳对齐」取代了 Qwen2.5-VL 的 T-RoPE 绝对时间位置，时间信息以文字形式进入序列，位置 id 里的 \( t \) 行几乎不再增长（证据见 4.2.3）。

#### 4.2.2 核心流程

本模块要回答的问题：**为什么 256K → 1M（比值 ≈ 3.81）官方却建议 factor 取 2 或 3？**

直觉：factor 的作用是把「实际会用到的最大位置 id」压回「原生 256K」以内。YaRN 需要的 factor ≈ 最大位置 id / 262144。**如果实际最大位置 id 远小于序列 token 数，factor 就可以远小于 token 数比值。**

MRoPE 下，一段视觉输入的位置 id 增长方式（对照 4.2.3 的源码）：

- 设一个视觉块在 LLM 侧的 token 网格为 \( t \times h \times w \)（块内 token 总数 \( N = t \cdot h \cdot w \)）；
- 块内三个位置行的取值范围分别只有 \( 0..t{-}1 \)、\( 0..h{-}1 \)、\( 0..w{-}1 \)；
- 这个块结束后，全局位置计数器只推进 \( \max(t, h, w) \)，而不是 \( N \)。

对 vanilla RoPE，同样 \( N \) 个 token 会推进位置 \( N \)。两者之比：

\[
\frac{\text{MRoPE 推进量}}{\text{vanilla 推进量}} = \frac{\max(t,h,w)}{t \cdot h \cdot w} \approx \frac{1}{\sqrt{N}} \quad(\text{当 } t{=}1,\ h{\approx}w)
\]

算一个具体例子（视频场景）：一段 300 帧视频，每帧在 LLM 侧是 64×64 的 token 网格。每个视觉块 \( N = 4096 \) 个 token、只推进位置 \( \max(64,64) = 64 \)。于是：

- token 总数：\( 300 \times 4096 \approx 1.23\text{M} \)（已超 1M 预算，这里只作数量级演示）
- vanilla RoPE 需要的位置范围：≈ 1.23M
- MRoPE 实际推进：\( 300 \times 64 = 19200 \)，再加上文本与时间戳 token 的逐个 +1

**结论**：以视觉为主的 1M token 序列，实际最大位置 id 可能只有几万，\( 262144 \times 3 \approx 786\text{K} \) 的覆盖能力绰绰有余；而 factor 取 4 会过度内插、无谓损失高频分辨率。这就是 README 那句官方提示的机制根源：

> 因为 Interleaved-MRoPE 的位置 id 增长得比 vanilla RoPE 慢，请使用**更小的缩放因子**。例如 256K 扩到 1M 时取 factor=2 或 3，而不是 4。

出处：[README.md:L795](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L795)。

一个诚实的边界说明：**纯文本**输入下 MRoPE 三行同增、与 vanilla RoPE 等效，最大位置 id 就等于序列长度。如果你的业务真是「满 1M 纯文本」，factor=3 是否够用属于模型效果问题，官方仓库未给出纯文本 1M 的实测承诺，建议以实际评测为准（待本地验证）。

#### 4.2.3 源码精读

仓库里最接近这层机制的真实代码是微调框架里的位置 id 复现实现 `get_rope_index_3`（它镜像了模型内部的行为，完整逐行精读安排在 u5-l4，这里只取与本讲相关的三处）。

**① 时间戳设计：视频按时间戳拆块、t 维置 1** —— [rope2d.py:L5-L18](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/rope2d.py#L5-L18)：

这段代码定义了 Qwen3-VL 的位置 id 计算（docstring 明言「Qwen3VL 使用时间戳而非绝对时间位置 id」），并在入口处把 `video_grid_thw` 按帧数 `repeat_interleave` 拆开、把每块的 t 维强制置 1——对应「`<t1> <vision_start> 帧1 <vision_end> <t2> <vision_start> 帧2 <vision_end>`」这种时间戳夹帧的序列结构。

**② t_index 恒为 0 的直接证据** —— [rope2d.py:L76-L90](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/rope2d.py#L76-L90)：

这段代码先算出视觉块在 LLM 侧的网格 `llm_grid_t/h/w`（除以 `spatial_merge_size`，即 2×2 空间合并），然后为视觉 token 生成三行位置 id：`t_index`/`h_index`/`w_index` 都是**从 0 开始的局部网格索引**，整体加上「前面文本长度 + 起始偏移」。其中源码注释直接写明：`t_index is always 0 because llm_grid_t is always 1 (we use timestamps to encode the temporal information for videos)`——t 行恒为 0，时间信息走文本时间戳。

**③ 位置推进量的来源** —— 同段代码中 `llm_pos_ids_list.append(torch.stack([t_index, h_index, w_index]) + text_len + st_idx)` 与下一块开始时 `st_idx = llm_pos_ids_list[-1].max() + 1` 的配合，意味着一个 \( t{\times}h{\times}w \) 的视觉块结束后，全局计数器只抬升 \( \max(t,h,w) \)。这就是 4.2.2 里那笔账的代码依据。

**④ factor 官方取值说明** —— [README.md:L795](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L795)：README 唯一一处对 factor 取值给出理由的地方，即 4.2.2 开头引用的那句「取 2 或 3，不是 4」。

#### 4.2.4 代码实践

**实践目标**：用 20 行纯 CPU 代码验证「MRoPE 位置推进慢于 vanilla RoPE」，把 4.2.2 的数学账变成可运行的事实。

**操作步骤**（示例代码）：

```python
# 示例代码：pos_growth_demo.py —— 只模拟位置计数器，不需要模型
def vanilla_pos(tokens):            # vanilla RoPE：位置 = token 数
    return tokens

def mrope_pos(num_blocks, h, w):    # 视觉块逐块推进 max(h, w)（Qwen3-VL t 恒 1）
    return num_blocks * max(h, w)

blocks, h, w = 300, 64, 64          # 300 帧，每帧 64×64 token 网格
tokens = blocks * h * w
print(f"token 总数        : {tokens:,}")            # 视觉 token 数
print(f"vanilla 位置范围  : {vanilla_pos(tokens):,}")
print(f"MRoPE 位置推进    : {mrope_pos(blocks, h, w):,}")
print(f"YaRN 需要的 factor(粗略) = 最大位置id / 262144")
for f in (2.0, 3.0, 4.0):
    cover = int(262144 * f)
    print(f"  factor={f}: 可覆盖位置约 {cover:,} -> "
          f"{'足够' if cover >= mrope_pos(blocks,h,w) else '浪费'}")
```

**需要观察的现象**：MRoPE 的位置推进量（19200）比 token 总数（1,228,800）小约 64 倍；factor=2 对这个视觉场景已经足够。

**预期结果**：控制台输出三行对比，直观呈现「位置 id 增长慢于 token 数」；如果改变 `h, w`（例如改成 32×32），观察推进比例按 \( 1/\sqrt{N} \) 量级变化。此脚本不依赖任何模型文件，可直接运行验证。

#### 4.2.5 小练习与答案

**练习 1**：`mrope_section: [24, 20, 20]` 中三个数字分别分配给谁？总和为什么是 64？
**答案**：依次是时间 t、高 h、宽 w 三段位置 id 各自控制的旋转维度数；旋转维度数是 head_dim 的一半（head_dim 128 时为 64），24+20+20=64 正好用满。取自己模型 config 中的原值为准。

**练习 2**：Qwen2.5-VL 的 MRoPE 与 Qwen3-VL 的 Interleaved-MRoPE，在「频率分配」上的差别是什么？
**答案**：Qwen2.5-VL 把三段维度连续切块（[24 | 20 | 20]），每段只能用各自对应的局部频段；Qwen3-VL 交错排列（t,h,w,t,h,w,…），t/h/w 三个维度都能获得从高频到低频的全频段覆盖，长序列下各维度的位置分辨能力更均衡。

**练习 3**：为什么 Qwen3-VL 里 `t_index` 可以恒为 0，而 Qwen2.5-VL 的 t 维必须随帧增长？
**答案**：Qwen3-VL 把时间信息显式编码为文本时间戳（`second_per_grid_ts` 那套机制被文本时间戳取代），时间不依赖位置 id 表达；Qwen2.5-VL 用 T-RoPE，把绝对时间写进 t 行位置 id，因此 t 维必须随时间步进（对比阅读 `rope2d.py` 中 `get_rope_index_25` 的 `time_tensor` 计算即可看到差异）。

### 4.3 vLLM 长上下文服务参数

#### 4.3.1 概念说明

改 `config.json` 适合「本地有一份模型目录」的场景；如果用 vLLM 起服务，还可以**不改文件、用命令行参数临时启用 YaRN**——好处是同一份权重可以在不同任务里按需切换上下文长度。README 在 YaRN 一节的末尾给出了这种用法（[README.md:L789-L793](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L789-L793)）。前置条件：`vllm >= 0.11.0` 才支持 Qwen3-VL（[README.md:L858](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L858)）。

#### 4.3.2 核心流程

两个关键参数（与 u3-l3 学过的 `vllm serve` 参数体系同名同义，Python 引擎下划线对应 CLI 连字符）：

- `--rope-scaling '<json>'`：内容就是 4.1.3 那块 `rope_scaling` 的 JSON 原样搬过来；
- `--max-model-len 1000000`：引擎允许的最大序列长度。**必须一起给**——不给的话引擎仍按 config 里的 262144 截断。

启动前先粗估 KV cache 是否放得下：

\[
\text{KV cache 字节} \approx 2 \times n_{\text{layer}} \times n_{\text{kv\_head}} \times d_{\text{head}} \times b \times L \times s
\]

其中 \( b \) 是精度字节数（bf16 为 2），\( L \) 序列长度，\( s \) 并发批大小，`num_hidden_layers`、`num_key_value_heads`、`head_dim` 都在你模型的 `config.json` 里。1M 长度 × 单并发往往就要吃掉几十 GB，量级不够时优先降 `--max-model-len`（比如 512K）或提高 `--gpu-memory-utilization`。

#### 4.3.3 源码精读

**① 官方 235B 的 1M 启动命令** —— [README.md:L789-L793](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L789-L793)：

```bash
vllm serve Qwen/Qwen3-VL-235B-A22B-Instruct --rope-scaling '{"rope_type":"yarn","factor":3.0,"original_max_position_embeddings": 262144,"mrope_section":[24,20,20],"mrope_interleaved": true}' --max-model-len 1000000
```

这段命令把 YaRN 的五个字段（rope_type / factor / original_max_position_embeddings / mrope_section / mrope_interleaved）经 `--rope-scaling` 传入，并用 `--max-model-len` 放开序列上限；注意命令行 JSON 里 `mrope_interleaved` 的值是 `true`（JSON 布尔，不是 Python 的 `True`）。

**② 版本要求** —— [README.md:L856-L868](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L856-L868)：官方推荐 vLLM 部署，并明确 `vllm>=0.11.0` 才支持 Qwen3-VL、安装命令为 `uv pip install -U vllm`。

**③ 与 u3-l3 参数的组合位置**：`--rope-scaling` 与 `--max-model-len` 可以和 u3-l3 讲过的 `--tensor-parallel-size`、`--mm-encoder-tp-mode data`、`--media-io-kwargs` 等任意组合（如 [README.md:L879-L887](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L879-L887) 的 235B FP8 命令所示）；多卡张量并行也是摊薄 1M KV cache 的常用手段。

#### 4.3.4 代码实践

**实践目标**：为 2B 小模型组装一条可执行的 1M 上下文服务命令，并验证 YaRN 已生效。

**操作步骤**：

1. 把 4.1.4 产出的 `./qwen3vl-2b-1m` 目录（已改好 config）作为模型路径，起服务：

```bash
# 示例命令：用改过 config 的本地目录（等价于命令行方式）
vllm serve ./qwen3vl-2b-1m \
  --max-model-len 1000000 \
  --gpu-memory-utilization 0.9
```

2. 或者不改文件、纯命令行启用（与上面二选一）：

```bash
# 示例命令：不改 config，命令行临时启用 YaRN
vllm serve Qwen/Qwen3-VL-2B-Instruct \
  --rope-scaling '{"rope_type":"yarn","factor":3.0,"original_max_position_embeddings":262144,"mrope_section":[24,20,20],"mrope_interleaved":true}' \
  --max-model-len 1000000
```

3. 用 u3-l3 学过的 OpenAI SDK 发一条长文本请求，观察服务端日志中的 `max_model_len`。

**需要观察的现象**：启动日志中模型信息一行应显示 1M 的上下文长度（而不是 262144）；不传 `--max-model-len` 时，超过 256K 的请求会被拒绝。

**预期结果**：服务正常启动并接受 >256K 的输入。待本地验证：1M 长度对显存要求很高（单条请求的 KV cache 就可能超出单卡），2B 模型若 OOM，先把 `--max-model-len` 降到 262144×2 观察行为。

#### 4.3.5 小练习与答案

**练习 1**：`--rope-scaling` 里的 JSON 有一个字段写错成 Python 风格 `True`，会发生什么？
**答案**：该参数按 JSON 解析，`True` 不是合法 JSON 布尔值，vLLM 启动时报参数解析错误（JSON decode error），服务起不来。应写 `true`。

**练习 2**：为什么 `--rope-scaling` 和 `--max-model-len` 必须成对出现？
**答案**：前者改变位置编码的频率表（让位置 id 能被正确编码），后者改变引擎允许的序列长度上限（让长输入能进得来）。只开前者，长请求仍被 262144 上限拦截；只开后者，超范围的位置 id 没有 YaRN 兜底，输出质量崩坏。

**练习 3**：同一份权重，白天业务只需 32K、夜间批处理要 512K，用哪种方式切换更合适？
**答案**：命令行方式。白天不带 `--rope-scaling` 起服务（原生 256K 足够，且注意力频率未被内插），夜间带参数重启；避免改 `config.json` 造成「一份目录一种配置」的管理负担。

### 4.4 FlashAttention-2 加速

#### 4.4.1 概念说明

FlashAttention 是长上下文的另一半答案。标准注意力的瓶颈不在算力而在**显存读写**：softmax 需要先算出完整 \( L \times L \) 分数矩阵、读写一遍再归一化。FlashAttention 把 Q/K/V 切成小块在 SRAM（片上高速缓存）里完成「乘—在线 softmax—累加」，从不在 HBM 里物化整个分数矩阵：

- 显存：\( O(L^2) \to \) 近似 \( O(L) \)；
- 速度：大幅减少 HBM 读写，多图/视频这类长序列场景收益最大；
- 精度：**精确注意力**，数学上与标准实现等价，不是近似算法。

Qwen3-VL 官方在 Quickstart 里就推荐开启：「We recommend enabling flash_attention_2 for better acceleration and memory saving, especially in multi-image and video scenarios」（[README.md:L152-L158](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L152-L158)——这段注释同时给出了带 `attn_implementation="flash_attention_2"` 的完整加载示例）。

#### 4.4.2 核心流程

启用三步：

```text
① 安装：pip install -U flash-attn --no-build-isolation
② 确认条件：兼容的 GPU（ Ampere 及以上架构，详见 flash-attention 官方仓库）；
            模型必须以 float16 / bfloat16 加载（fp32 不支持）
③ 加载：from_pretrained(..., torch_dtype=torch.bfloat16,
                            attn_implementation="flash_attention_2")
```

两个易踩的坑：

- `dtype="auto"` 时如果 config 里是 fp32 权重，开 `flash_attention_2` 会因精度不符报错——显式给 `torch_dtype=torch.bfloat16` 最稳；
- 该参数是 **transformers 加载路径**的开关；vLLM/SGLang 引擎内部自管注意力后端（如 SGLang 示例中的 `attention_backend="fa3"`，即 FlashAttention-3，见 [README.md:L1117-L1125](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L1117-L1125)），不需要也不接受这个参数。

#### 4.4.3 源码精读

**① 官方启用说明与代码** —— [README.md:L740-L761](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L740-L761)：

这一节是仓库内关于 FlashAttention-2 的权威说明：先用 `pip install -U flash-attn --no-build-isolation` 安装最新版（L742-L746）；接着强调两个前提——需要兼容的硬件（细节指向 [flash-attention 仓库](https://github.com/Dao-AILab/flash-attention)），且只能用于 `torch.float16` 或 `torch.bfloat16` 加载的模型（L748）；最后给出加载代码：`attn_implementation="flash_attention_2"` 配合 `torch_dtype=torch.bfloat16`（L752-L761）。

**② 工程入口的真实用法** —— [web_demo_mm.py:L34-L37](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L34-L37)：

Web 演示脚本提供了 `--flash-attn2` 布尔开关（`help='Enable flash_attention_2 when loading the model.'`），把 README 的建议落成了命令行参数。

**③ 加载分支** —— [web_demo_mm.py:L95-L102](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L95-L102)：

这段代码在 hf 后端加载模型时检查该开关：开启则 `attn_implementation='flash_attention_2'`，否则按默认注意力实现加载。结合 u1-l4 的结论——该开关只作用于 hf 分支，vllm 分支由引擎自管注意力，不受此参数影响。

#### 4.4.4 代码实践

**实践目标**：在真实工程入口上体验 FlashAttention-2 的开启路径，并建立「开与关」的对照意识。

**操作步骤**：

1. 安装：`pip install -U flash-attn --no-build-isolation`（编译耗时较长，属于正常现象）。
2. 用 u1-l4 学过的命令启动 Web 演示（示例命令）：

```bash
python web_demo_mm.py -c /path/to/Qwen3-VL-2B-Instruct --flash-attn2
```

3. 上传一段视频提问（视频输入的注意力矩阵最长，收益最明显），记录首 token 延迟与 `nvidia-smi` 的显存峰值；再去掉 `--flash-attn2` 重复一次。

**需要观察的现象**：开启后显存峰值下降、长输入（视频/多图）响应更快；日志/终端无 fp32 与 flash-attn 不兼容的报错（若报错，检查权重 dtype）。

**预期结果**：两次运行答案一致（FlashAttention 是精确实现，结果只受随机性影响），资源占用不同。待本地验证：具体加速比依赖 GPU 型号与输入长度，请以本机实测数据为准。

**无 GPU 的替代实践（源码阅读型）**：打开 [web_demo_mm.py:L95-L102](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L95-L102)，确认 `attn_implementation` 只出现在 hf 分支；再对照 [README.md:L752-L761](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L752-L761) 的官方示例，写出「transformers 手写推理脚本」中启用 FlashAttention-2 的那三行加载代码。

#### 4.4.5 小练习与答案

**练习 1**：FlashAttention-2 与「近似注意力」（如滑窗注意力）的本质区别？
**答案**：FlashAttention 是精确算法——通过分块与在线 softmax 避免物化 \( L \times L \) 矩阵，计算结果与标准注意力数学等价；滑窗等方法牺牲远距离注意力来换速度，是有损的。

**练习 2**：为什么模型以 fp32 加载时不能开 `flash_attention_2`？
**答案**：FlashAttention-2 内核只为 float16/bfloat16 实现（fp32 会显著增加 SRAM 压力、失去加速意义），transformers 在加载时会校验 dtype 并报错。这也是官方示例同时写 `torch_dtype=torch.bfloat16` 的原因。

**练习 3**：vLLM 起服务时要不要传 `attn_implementation="flash_attention_2"`？
**答案**：不要。该参数属于 transformers 的 `from_pretrained` 加载路径；vLLM/SGLang 引擎内部自管注意力后端（SGLang 通过 `attention_backend` 选择，如 `fa3`），传这个参数没有意义。CLI 上也根本没有这个旗标。

## 5. 综合实践

把本讲四个模块串成一个「**1M 上下文加速推理服务**」小项目，对应本讲规格中的实践任务：

1. **准备 config**（4.1）：用 4.1.4 的脚本，基于 `Qwen/Qwen3-VL-2B-Instruct` 的 `config.json` 生成 `./qwen3vl-2b-1m`，修改 `max_position_embeddings`、`rope_scaling`（含 `mrope_interleaved: true`、`factor: 3.0`），保留备份。
2. **自查 factor 合理性**（4.2）：运行 4.2.4 的 `pos_growth_demo.py`，代入你实际业务中最长的视频规格（帧数 × 网格），估算 MRoPE 位置推进量，写下「factor=3 覆盖了我的场景」或「需要实测调参」的结论。
3. **起服务**（4.3）：用改过 config 的目录启动 `vllm serve ./qwen3vl-2b-1m --max-model-len 1000000`；OOM 则按 4.3.2 的 KV cache 公式回算一个可行的 `--max-model-len`（如 524288），记录推导过程。
4. **加速对照**（4.4）：再写一个 transformers 手写推理脚本（u1-l3 的三步调用链），分别在带与不带 `attn_implementation="flash_attention_2"` 两种加载方式下跑同一段长视频输入，记录显存与耗时。
5. 把以上四步的命令、输出和结论整理成一页 `notes.md`——这份笔记就是你在自己机器上复现「Qwen3-VL 1M 长上下文 + FlashAttention 加速」的操作手册。

所有涉及 GPU 的步骤若暂无条件执行，注明「待本地验证」并保留命令与预期现象的书面推导。

## 6. 本讲小结

- 256K → 1M 靠 **YaRN**：训练零改动，只改 `config.json` 的 `max_position_embeddings` 与 `rope_scaling`（`rope_type: yarn` + `original_max_position_embeddings: 262144` + `factor` + MRoPE 两字段）。
- YaRN 的本质是**按波长分组**的频率缩放（高频保外推、低频做内插、中间平滑过渡）加注意力温度校正，优于均匀内插（PI）。
- factor 取 **2~3 而非 4**：Interleaved-MRoPE 下视觉块只把位置计数器推进 \( \max(t,h,w) \)（且 Qwen3-VL 的 t 恒为 0，时间走文本时间戳），实际最大位置 id 远小于 token 数，过度缩放反而损失分辨率。
- vLLM 侧用 **`--rope-scaling` + `--max-model-len` 成对**开启，`vllm>=0.11.0`；改 config 与命令行两种方式按「是否需要随任务切换」取舍。
- **FlashAttention-2** 通过分块计算把注意力显存从 \( O(L^2) \) 降到近似 \( O(L) \) 且结果精确；transformers 路径用 `attn_implementation="flash_attention_2"`（需 fp16/bf16 + 兼容 GPU），vLLM/SGLang 引擎自管注意力后端。
- 长上下文的两大代价——KV cache 显存与注意力开销——分别用「`--max-model-len` 规划 + 张量并行」和「FlashAttention」化解。

## 7. 下一步学习建议

本讲是单元三的最后一讲。位置编码的机制在本讲只是「点到为止」：

- **想彻底吃透 MRoPE**：进入 u5-l4（`rope2d.py` 三代实现对比），那里会逐行精读 `get_rope_index_2/25/3`，亲手构造 `input_ids` 打印 3×L 的位置矩阵，并对比 Qwen2.5-VL 的 T-RoPE 时间步进与 Qwen3-VL 时间戳设计的差异。
- **想看 FlashAttention 在训练中的用法**：u5-l6（trainer.py）讲解微调框架如何用 `flash_attn_varlen_func` + `cu_seqlens` 实现多样本拼接下的样本间注意力隔离，是本讲推理侧加速的训练侧对应物。
- **想跑长视频的端到端效果**：u6-l3（VideoMME 评测）提供数小时视频的完整推理脚本，正好是 1M 上下文 + 时间戳机制的实战检验场。
- **延伸阅读**：YaRN 论文（arXiv:2309.00071）与 flash-attention 仓库（README L740 一节给出的官方链接）。
