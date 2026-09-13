# 二次开发：把 Block AttnRes 移植进你自己的模型

## 1. 本讲目标

前面十二讲沿着一条主线走完：u1 单元认识了标准残差的问题与 AttnRes 的核心公式，u2 单元把 README 伪代码逐行读透（u2-l1 的 `block_attn_res`、u2-l2 的块调度、u2-l3 的组件与开销），并在自建实验台上完成了对比训练与训练动态观测（u2-l4、u2-l5）；u3 单元做了复杂度与显存账（u3-l1）、scaling law 与下游评测解读（u3-l2、u3-l3）以及论文精读与复现规划（u3-l4）。但这些工作有一个共同起点：**实验台是我们自己搭的**——u2-l4 的 `MiniGPT` 从第一行代码起就是为双状态 `(blocks, partial_block)` 设计的。

真实世界的二次开发几乎总是相反的顺序：你面对的是一个**已经存在、从未为 AttnRes 设计**的代码库（nanoGPT、HuggingFace 风格的实现、自家训练框架），几千行代码里混着 dropout、RoPE、MoE、梯度检查点、`torch.compile`……你的任务是只动该动的地方，把 README 伪代码嵌进去，并且**不破坏其余一切**。本讲是手册的收官讲，把前面所有零件组装成一项可迁移的工程能力。学完本讲，你应该能够：

1. 在**任意** PreNorm Transformer 代码库中定位 AttnRes 的集成改造点：层签名、两个 attn_res 位点、模型外壳——共三类改动，子层与其余部分零改动；
2. 用「三问分诊法」判断目标库中的每个组件（RoPE、MoE、PostNorm、KV cache……）与 AttnRes 的兼容性，分清「正交兼容」「需要适配」「不在论文设定内」；
3. 执行一份四级上线前检查清单（结构 → 训练健康 → 统计纪律 → 工程适配），并用 `preflight()` 函数把结构检查全部自动化；
4. 独立完成一次完整移植：把 Block AttnRes 移植进一个开源迷你 GPT（nanoGPT 风格），保持参数量基本对齐、训练收敛，并留下可复用的移植步骤与踩坑笔记。

README 对 AttnRes 的定位是本讲的「契约原文」——它是一个「drop-in replacement for standard residual connections」（[README.md:L33](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L33)）。u2-l4 已经把「drop-in」翻译成「同一个类 + 一个模式开关」；本讲要把它翻译成**对别人代码库的一份最小 diff**。

## 2. 前置知识

### 2.1 移植与自建：为什么这一讲更接近真实工作

| | u2-l4 自建实验台 | 本讲的移植任务 |
|:---|:---|:---|
| 起点 | 白纸，为双状态设计 | 已有代码库，为单流设计 |
| 层签名 | 一开始就是 `forward(self, blocks, hidden)` | 必须从 `forward(self, x)` 改过来 |
| 组件构成 | 最小集（MHA + MLP + RMSNorm） | dropout、RoPE、MoE、检查点、编译……都可能在场 |
| 主要风险 | 写错（漏掉状态传递） | **改错**：破坏原有语义、漏适配组件 |
| 核心技能 | 装配 | 定位 + 克制：只改该改的三类地方 |

「drop-in」在工程上的准确含义不是「零改动」，而是**改动面小、局部、可枚举**。本讲的第一项工作就是把这句话变成一份可以逐条打勾的 diff 清单。

### 2.2 迷你 GPT 的解剖：残差流藏在哪里

nanoGPT（外部开源项目 karpathy/nanoGPT）的 Block 是几乎所有迷你 GPT 的公共祖先形态。下面按其公开结构**简化改写**（去掉 dropout、weight tying 等与本节无关的部分，只保留骨架；具体行号以你手上的版本为准，待确认）：

```python
# 示例代码：nanoGPT 风格 Block（依据公开结构简化改写，非本仓库代码）
class Block(nn.Module):
    def forward(self, x):
        x = x + self.attn(self.ln_1(x))   # ← AttnRes 位点 1 藏在这行里
        x = x + self.mlp(self.ln_2(x))    # ← AttnRes 位点 2 藏在这行里
        return x
```

这两行 `x = x + ...` 就是残差流的全部接线——u1-l2 讲过的递推 \(h_l = h_{l-1} + v_l\) 在代码里就长这个样子。在陌生代码库里定位它们，搜三类模式即可：

| 搜索模式 | 常见于 |
|:---|:---|
| `x = x + ` / `hidden = hidden + ` | nanoGPT 及多数教学实现 |
| `residual = hidden_states` 后再相加 | HuggingFace Llama / GPT-NeoX 系 |
| `hidden_states +=`（原地写法） | 各家训练框架 |

找到这两行，就找到了位点 1 与位点 2；找到包裹它们的循环与嵌入查找，就找到了第三类改造点（模型外壳）。

### 2.3 从前几讲随身携带的五条不变式

本讲反复使用以下结论（推导见对应讲义）：

| # | 不变式 | 出处 |
|:---:|:---|:---|
| I1 | 双状态线程化：层签名为 `(blocks, hidden) -> (blocks, partial)`，张量重绑定不返回即丢失 | u2-l2 |
| I2 | `layer_number` 按 **0 起点**计数；块边界为 `layer_number % (block_size // 2) == 0`；`block_size` 按 ATTN+MLP **子层**计数（每层 2 个） | u2-l2 |
| I3 | `blocks` 每次前向重置；词嵌入（含位置信息）在第 0 层被封存为 `blocks[0]` | u2-l4 |
| I4 | 候选数闭式：\(C_{attn}(l) = \lfloor (l+k-1)/k \rfloor + 1\)，\(C_{mlp}(l) = \lfloor l/k \rfloor + 2\)，其中 \(k = \text{block\_size}//2\) | u2-l4 |
| I5 | 参数增量恰为每层 \(4d\)（两位点 × (proj 的 \(d\) + norm 增益的 \(d\)）)，全模型 \(4dL\) | u2-l3 |

### 2.4 符号表与一个容易混淆的点

| 符号 | 含义 | 本讲冒烟配置 |
|:---:|:---|:---:|
| d / L | 隐藏维度 / 层数 | 64 / 8 |
| k | 每块层数 = block_size // 2 | 2 |
| N | 末端已封存块数（含嵌入块） | L/k = 4 |
| ln_1 / ln_2 | **目标库自己的**子层入口 Norm（扮演伪代码中 attn_norm / mlp_norm 的角色） | LayerNorm |
| attn_res_norm | **新增的**位点内对 K 的 RMSNorm（README 类型签名指定） | RMSNorm |

提前澄清一个移植时最容易「顺手搞错」的点：`ln_1/ln_2` 与 `attn_res_norm/mlp_res_norm` 是**两回事**。前者决定子层读到的尺度，属于目标库，**保留原样**（是 LayerNorm 就让它继续是 LayerNorm）；后者决定深度打分的键，README 伪代码的类型签名固定为 RMSNorm（[README.md:L53](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L53)），u2-l3 讲过它「只看方向不看幅度」的理由。移植时不要「统一风格」而把它们互换。

## 3. 本讲源码地图

本仓库是论文发布仓库，没有任何工程代码（u1-l1 已确认）。本讲可引用的源码仍以 README 伪代码为集成蓝图；移植目标 nanoGPT 是外部开源项目，其代码在正文中一律以「示例代码（依据公开结构改写）」出现，不给行号（待确认）。

| 位置 | 内容 | 本讲用途 |
|:---|:---|:---|
| [README.md:L33](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L33) | AttnRes 定位：drop-in 替换 | 4.1：移植契约的出处 |
| [README.md:L37](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L37) | 问题陈述以 PreNorm 为背景 | 4.2：PostNorm 兼容性判断 |
| [README.md:L47](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L47) | ~8 块、边际开销、实用 drop-in | 4.1 配置依据 / 4.3 达标线 |
| [README.md:L52-L91](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L52-L91) | PyTorch 风格伪代码全块 | 全讲：移植蓝图 |
| [README.md:L53-L65](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L53-L65) | `block_attn_res` 计算单元 | 4.1：原样搬运，零修改 |
| [README.md:L67-L68](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L67-L68) | 层签名与 partial 起点 | 4.1：签名改造 |
| [README.md:L70](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L70) | blocks already include token embedding | 4.1：外壳改造 |
| [README.md:L71](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L71) / [README.md:L84](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L84) | 两个 attn_res 位点 | 4.1：位点改造 |
| [README.md:L74-L77](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L74-L77) | 块边界判断与封存（子层计数） | 4.1：调度改造 |
| [README.md:L80-L81](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L80-L81) / [README.md:L87-L88](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L87-L88) | 子层两行（零改动对象） | 4.1 / 4.2：兼容契约 |
| [README.md:L90](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L90) | 双状态返回 | 4.1 |
| [README.md:L105](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L105) | 下游证据出自 MoE + 线性注意力底座 | 4.2：组件兼容的大规模证据 |
| [README.md:L121-L123](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L121-L123) | 训练动态：幅度有界、梯度均匀 | 4.3：移植后的健康回归测试 |
| 外部：karpathy/nanoGPT | 移植目标（示例改写，无行号，待确认） | 4.1 / 5 |

## 4. 核心概念与源码讲解

本讲的三个最小模块按一次真实移植的时间顺序展开：

1. **4.1 集成改造点**——先回答「改哪里」：三类改动 + 一个可选开关，产出一份适用于任何 PreNorm 代码库的移植 diff；
2. **4.2 组件兼容性检查**——再回答「会不会撞车」：用三问分诊法逐一检查目标库里的组件，判断它与 AttnRes 的相互作用；
3. **4.3 上线前检查清单**——最后回答「凭什么敢开训」：四级检查清单与 `preflight()` 自动化。

### 4.1 集成改造点：从单流骨架到双状态的三类改动

#### 4.1.1 概念说明

把 README 伪代码与 2.2 节的 nanoGPT 骨架并排放在一起，会发现一件颇具启发性的事：**伪代码里所有「陌生的行」都不属于子层**。注意力与 MLP 各只出现一次、各占一行（L80、L87），其余全部是残差接线的编排。这就是移植契约的来源：

**移植契约（改什么）**——三类改动，共约 15 行：

| 类别 | 改动内容 | 对应伪代码 |
|:---:|:---|:---|
| ① 层签名 | `forward(self, x)` → `forward(self, blocks, hidden_states)`，返回 `(blocks, partial)` | [README.md:L67](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L67)、[README.md:L90](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L90) |
| ② 两个位点 | 把 `x = x + self.attn(...)` 一行拆成「聚合 → 边界 → 子层 → 并回」四步，MLP 同理；新增两组 proj/norm 参数 | [README.md:L68-L81](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L68-L81)、[README.md:L84-L88](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L84-L88) |
| ③ 模型外壳 | `blocks = []` 每次前向重置；循环改双状态搬运；末端 `ln_f` 接最后的 partial | [README.md:L67](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L67)、[README.md:L70](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L70) |

**移植契约（不改什么）**——五个「零改动」：

1. 子层模块内部（`self.attn`、`self.mlp` 的全部代码）零改动；
2. 子层入口 Norm（`ln_1`/`ln_2`）零改动——它们就是伪代码里的 `attn_norm`/`mlp_norm`，只是名字不同；
3. 嵌入查找、损失函数、LM 头的模块定义零改动（LM 头只换读取对象：读最后的 partial）；
4. 数据管线、优化器、评测代码零改动；
5. 唯一新增参数是每层的 \(4d\)（I5），唯一新增超参是 `block_size`。

还有一个强烈建议保留的**可选第四类改动**：一个 `use_attnres` 布尔开关，让「接好线但不聚合」（`h = partial`）的原版形态与完整 AttnRes 共存于同一个类。u2-l4 用 `mode` 开关实现了它；移植场景里它的价值更大——既是排障脚手架（§5 的 S2 步），也是日后一切对比实验的基线。

#### 4.1.2 核心流程

七步移植协议（每步的即时验证见 §5.2 操作单）：

```text
S1 定位残差流   在目标库中找到「x = x + 子层(Norm(x))」的两行与外壳循环
S2 改层签名     forward(self, x) → forward(self, blocks, hidden_states)
               并在外壳把 (blocks, x) 逐层搬运; 此时可加 use_attnres 开关,
               开关关闭时行为与原版完全一致
S3 装参数       每层新增 attn_res_proj/attn_res_norm 与 mlp_res_proj/mlp_res_norm
               (可选: proj 零初始化, 见 4.1.3 末尾)
S4 接位点 1     h = block_attn_res(blocks, partial, attn_res_*)
               attn 改读 self.attn(self.ln_1(h)); 输出并回 partial
S5 接边界       layer_number % (block_size // 2) == 0 时封存 partial 并置 None
               (0 起点计数; block_size 按子层计, 每层 2 个)
S6 接位点 2     h = block_attn_res(blocks, partial, mlp_res_*)
               mlp 读 self.mlp(self.ln_2(h)); 输出并回 partial; 返回 (blocks, partial)
S7 末端接线     外壳 blocks=[] 每前向重置; 第 0 层自动把嵌入封存为 blocks[0];
               末端 ln_f 接最后的 partial
```

目标库原代码与移植后的**逐行对应表**（这张表就是移植 diff 的「说明书」）：

| 目标库原行（nanoGPT 风格） | 移植后 | 对应伪代码 | 动作 |
|:---|:---|:---:|:---:|
| `def forward(self, x):` | `def forward(self, blocks, hidden_states):` | L67 | 签名 |
| — | `partial = hidden_states` | L68 | 拆行 |
| — | `h = block_attn_res(blocks, partial, self.attn_res_proj, self.attn_res_norm)` | L71 | 位点 1 |
| — | 边界判断与封存三行 | L75-L77 | 调度 |
| `x = x + self.attn(self.ln_1(x))` | `attn_out = self.attn(self.ln_1(h))` + 并回 | L80-L81 | 拆行 |
| — | `h = block_attn_res(blocks, partial, self.mlp_res_proj, self.mlp_res_norm)` | L84 | 位点 2 |
| `x = x + self.mlp(self.ln_2(x))` | `partial = partial + self.mlp(self.ln_2(h))` | L87-L88 | 拆行 |
| `return x` | `return blocks, partial` | L90 | 签名 |
| `x = wte(idx) + wpe(pos)` | 不变；其结果经第 0 层封存进 `blocks[0]` | L70 | 外壳 |
| `for block in self.h: x = block(x)` | `blocks = [];` 循环改 `blocks, x = block(blocks, x)` | L67 | 外壳 |
| `logits = lm_head(ln_f(x))` | 不变（`x` 此时是最后的 partial） | —（模型级，README 未给，待确认） | 外壳 |

规模感：层内净增约 10 行，外壳净增 3 行；参数增量 \(4dL\)（d=384、L=12 的 nanoGPT 规模约 +18K 参数，占主干 < 0.1%）。这就是 L33「drop-in」在 diff 尺度上的样子。

#### 4.1.3 源码精读

**层签名是移植的第一刀，也是「能不能装」的判据**：

> [README.md:L67](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L67)
> `def forward(self, blocks: list[Tensor], hidden_states: Tensor) -> tuple[list[Tensor], Tensor]:`

状态从参数进、从返回值出（I1）。凡是把层塞进 `nn.Sequential` 的目标库，外壳都必须改成显式循环——这是判断「目标库是否容易移植」的第一个信号。

**两个位点是全部的计算改动**：

> [README.md:L71](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L71)
> `h = block_attn_res(blocks, partial_block, self.attn_res_proj, self.attn_res_norm)`
>
> [README.md:L84](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L84)
> `h = block_attn_res(blocks, partial_block, self.mlp_res_proj, self.mlp_res_norm)`

注意两次调用传的是**各自**的 proj/norm——u2-l3 已说明两位点各持一套独立参数、互不共享。

**子层两行「原文照抄」**：

> [README.md:L80](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L80)
> `attn_out = self.attn(self.attn_norm(h))`
>
> [README.md:L87](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L87)
> `mlp_out = self.mlp(self.mlp_norm(h))`

移植到 nanoGPT 时这两行变成 `self.attn(self.ln_1(h))`、`self.mlp(self.ln_2(h))`——**只换名字，不换结构**。子层对 `h` 的来源一无所知，这是 4.2 组件兼容性的根基。

**边界三行的计数口径**：

> [README.md:L74-L77](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L74-L77)
> `# block_size counts ATTN + MLP; each transformer layer has 2` / `if self.layer_number % (self.block_size // 2) == 0:` / `blocks.append(partial_block)` / `partial_block = None`

`//2` 这一下是移植中最常见的笔误来源（§5.3 踩坑表第 2 行）；`layer_number` 的 0 起点是第二个（§5.3 第 1 行）。

**嵌入入块的保证**：

> [README.md:L70](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L70)
> `# blocks already include token embedding`

u2-l2 推导过：第 0 层边界条件 `0 % k == 0` 恒真，嵌入（含位置嵌入）在进入第一个注意力前就被封存。移植时不需要为此写任何专门代码——**外壳 `blocks = []` 起步 + 第 0 层边界自动实现它**；要做的只是别把 `blocks = []` 初始化挪到 `__init__` 里（I3）。

**关于初始化的一个可选项**：`nn.Linear(d, 1, bias=False)` 的默认初始化已使初始深度权重近均匀（u2-l3）；若要**精确均匀**（\(h\) 恰为候选均值），可把 proj 权重零初始化——logits 全零 ⇒ softmax 全 \(1/(N{+}1)\)。u3-l4 精读时发现论文使用了零初始化（细节以论文为准，待确认）。两种初始化在 PreNorm 包裹下都安全（u2-l4 4.3 的尺度论证）。

#### 4.1.4 代码实践：动手移植一个 nanoGPT 风格模型

1. **实践目标**：把一份**单流**的 nanoGPT 风格迷你模型（下方「移植前」）改造成双状态 Block AttnRes 版（「移植后」），随后做三个结构检查：参数增量恰为 \(4dL\)、前向形状正确、初始损失 ≈ \(\ln V\)。
2. **操作步骤**：

```python
# 示例代码：移植目标（依据 nanoGPT 公开结构简化改写，非本仓库代码）
import torch, torch.nn as nn, torch.nn.functional as F

class NanoAttention(nn.Module):
    def __init__(self, d, n_head):
        super().__init__()
        self.n_head, self.hd = n_head, d // n_head
        self.c_attn = nn.Linear(d, 3 * d, bias=False)
        self.c_proj = nn.Linear(d, d, bias=False)
    def forward(self, x):                                   # [B, T, D]
        B, T, D = x.shape
        q, k, v = self.c_attn(x).split(D, dim=2)
        q, k, v = (t.view(B, T, self.n_head, self.hd).transpose(1, 2)
                   for t in (q, k, v))
        att = (q @ k.transpose(-2, -1)) / self.hd ** 0.5
        mask = torch.triu(torch.ones(T, T, dtype=torch.bool,
                                     device=x.device), 1)
        y = F.softmax(att.masked_fill(mask, float('-inf')), -1) @ v
        return self.c_proj(y.transpose(1, 2).reshape(B, T, D))

class NanoMLP(nn.Module):
    def __init__(self, d):
        super().__init__()
        self.c_fc = nn.Linear(d, 4 * d, bias=False)
        self.c_proj = nn.Linear(4 * d, d, bias=False)
    def forward(self, x):
        return self.c_proj(F.gelu(self.c_fc(x)))

class NanoBlock(nn.Module):          # ← 移植前：单流签名
    def __init__(self, d, n_head):
        super().__init__()
        self.ln_1, self.attn = nn.LayerNorm(d), NanoAttention(d, n_head)
        self.ln_2, self.mlp = nn.LayerNorm(d), NanoMLP(d)
    def forward(self, x):
        x = x + self.attn(self.ln_1(x))
        x = x + self.mlp(self.ln_2(x))
        return x

class NanoGPT(nn.Module):
    def __init__(self, vocab, d, n_head, n_layer, t_max):
        super().__init__()
        self.wte, self.wpe = nn.Embedding(vocab, d), nn.Embedding(t_max, d)
        self.h = nn.ModuleList(NanoBlock(d, n_head) for _ in range(n_layer))
        self.ln_f, self.lm_head = nn.LayerNorm(d), nn.Linear(d, vocab, bias=False)
    def forward(self, idx, targets=None):
        B, T = idx.shape
        x = self.wte(idx) + self.wpe(torch.arange(T, device=idx.device))
        for block in self.h:                     # ← 单流循环（改造点 ③）
            x = block(x)
        logits = self.lm_head(self.ln_f(x))
        if targets is None:
            return logits, None
        loss = F.cross_entropy(logits.reshape(-1, logits.size(-1)),
                               targets.reshape(-1))
        return logits, loss
```

```python
# 示例代码：移植后（新增/改动的行均以注释标出）
class RMSNorm(nn.Module):                        # README L53 类型签名指定的 norm
    def __init__(self, d, eps=1e-6):
        super().__init__()
        self.weight = nn.Parameter(torch.ones(d))
        self.eps = eps
    def forward(self, x):
        rms = x.pow(2).mean(-1, keepdim=True).add(self.eps).rsqrt()
        return x * rms * self.weight

def block_attn_res(blocks, partial_block, proj, norm):   # README L53-L65 原样搬运
    V = torch.stack(blocks + [partial_block])            # [N+1, B, T, D]
    K = norm(V)
    logits = torch.einsum('d, n b t d -> n b t', proj.weight.squeeze(), K)
    h = torch.einsum('n b t, n b t d -> b t d', logits.softmax(0), V)
    return h

class NanoBlockAR(nn.Module):
    def __init__(self, d, n_head, layer_number, block_size,
                 use_attnres=True, attn_cls=NanoAttention):   # 注入为 4.2 预留
        super().__init__()
        self.use_attnres = use_attnres
        self.layer_number, self.block_size = layer_number, block_size
        self.ln_1, self.attn = nn.LayerNorm(d), attn_cls(d, n_head)  # 原样保留
        self.ln_2, self.mlp = nn.LayerNorm(d), NanoMLP(d)            # 原样保留
        if use_attnres:                                   # ← 新增全部参数（4d/层）
            self.attn_res_proj = nn.Linear(d, 1, bias=False)
            self.attn_res_norm = RMSNorm(d)
            self.mlp_res_proj = nn.Linear(d, 1, bias=False)
            self.mlp_res_norm = RMSNorm(d)
            nn.init.zeros_(self.attn_res_proj.weight)     # 可选: 精确均匀起点
            nn.init.zeros_(self.mlp_res_proj.weight)      # (论文细节, 待确认)

    def forward(self, blocks, hidden_states):             # ← 改造 ①: 签名
        partial = hidden_states                           # L68
        if self.use_attnres:                              # ← 改造 ②: 位点 1
            h = block_attn_res(blocks, partial,
                               self.attn_res_proj, self.attn_res_norm)  # L71
        else:
            h = partial                                   # S2 的原版形态
        if self.use_attnres and \
           self.layer_number % (self.block_size // 2) == 0:   # L75 (0 起点计数)
            blocks, partial = blocks + [partial], None        # L76-L77
        attn_out = self.attn(self.ln_1(h))                # L80: 仅 x → h
        partial = attn_out if partial is None else partial + attn_out  # L81
        if self.use_attnres:                              # ← 改造 ②: 位点 2
            h = block_attn_res(blocks, partial,
                               self.mlp_res_proj, self.mlp_res_norm)   # L84
        else:
            h = partial
        partial = partial + self.mlp(self.ln_2(h))        # L87-L88
        return blocks, partial                            # L90

class NanoGPTAR(nn.Module):
    def __init__(self, vocab, d, n_head, n_layer, block_size, t_max,
                 use_attnres=True, attn_cls=NanoAttention):
        super().__init__()
        self.wte, self.wpe = nn.Embedding(vocab, d), nn.Embedding(t_max, d)
        self.h = nn.ModuleList(
            NanoBlockAR(d, n_head, i, block_size, use_attnres, attn_cls)
            for i in range(n_layer))                      # i 即 0 起点的 layer_number
        self.ln_f, self.lm_head = nn.LayerNorm(d), nn.Linear(d, vocab, bias=False)
    def forward(self, idx, targets=None):
        B, T = idx.shape
        x = self.wte(idx) + self.wpe(torch.arange(T, device=idx.device))
        blocks = []                                       # ← 改造 ③: 每前向重置
        for block in self.h:
            blocks, x = block(blocks, x)                  # ← 改造 ③: 双状态搬运
        logits = self.lm_head(self.ln_f(x))               # 末端读最后的 partial
        if targets is None:
            return logits, None
        loss = F.cross_entropy(logits.reshape(-1, logits.size(-1)),
                               targets.reshape(-1))
        return logits, loss

# ---- 三个结构检查 ----
cnt = lambda m: sum(p.numel() for p in m.parameters())
V, D, L, BS = 65, 64, 8, 4
torch.manual_seed(0)
orig = NanoGPT(V, d=D, n_head=4, n_layer=L, t_max=128)
port = NanoGPTAR(V, d=D, n_head=4, n_layer=L, block_size=BS, t_max=128)

print("参数增量:", cnt(port) - cnt(orig), " 理论 4dL =", 4 * D * L,
      f" 相对 {100*(cnt(port)-cnt(orig))/cnt(orig):.3f}%")     # 检查 1

idx = torch.randint(0, V, (2, 64)); tgt = torch.randint(0, V, (2, 64))
with torch.no_grad():
    logits, loss = port(idx, tgt)
print("输出形状:", tuple(logits.shape), " 初始损失:",
      f"{loss.item():.4f}", " ln(V) =", f"{torch.log(torch.tensor(float(V))):.4f}")
```

3. **需要观察的现象**：参数增量打印恰为 `4*64*8 = 2048`；输出形状 `[2, 64, 65]`；初始损失与 \(\ln 65 \approx 4.17\) 在两位小数内一致。
4. **预期结果**：检查 1 是结构决定的，应严格相等（注意 `NanoGPTAR` 与 `NanoGPT` 的主干超参完全一致才有可比性）；初始损失检查对随机目标同样成立（近均匀预测的交叉熵与目标分布无关）。若参数增量不等，先核对 `RMSNorm` 是否恰有 \(d\) 个参数、两个 `Linear(d, 1, bias=False)` 是否各恰有 \(d\) 个参数。损失数值待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：在一个陌生代码库里，你会先搜索什么来定位残差流？为什么这一步排在七步协议的第一位？

> **答案**：搜 `x = x + `、`residual = `、`hidden_states +=` 三类模式（2.2 节的表）。因为三类改动（签名、位点、外壳）全部锚定在残差流的两行接线及其外围循环上——先找到流，后面每一步才知道往哪儿改；找不到这两行（比如流被封装在框架内部），说明移植成本远高于预期，应先重估。

**练习 2**：移植后的 `Block.forward` 为什么必须返回 `(blocks, partial)`，而不能像原版一样只返回一个张量？

> **答案**：Python 的局部赋值不会传播给调用方：层内 `partial = None`、`blocks = blocks + [partial]` 都是对局部名字的**重绑定**，若不放进返回值，这些变化在层结束时就丢失（u2-l2 的「重绑定不返回即丢失」）。双状态必须由外壳逐层显式搬运，这也是外壳必须改成显式循环、不能再用 `nn.Sequential` 的原因。

**练习 3**：有人把位点 2 写成了 `partial = h + self.mlp(self.ln_2(h))`（用聚合结果 `h` 而不是 `partial` 作累加基底）。这错在哪？会通过 4.3 节的哪条检查暴露出来？

> **答案**：错在破坏了部分和的「只由子层输出累加而成」的 write-once 语义（u2-l2）。`partial` 应严格等于本块内子层输出之和（L81、L88 都是 `partial + 子层输出`）；把 `h` 写回后，部分和混入了跨块聚合的凸组合，后续封存的块不再是「纯块表示」，且当前块信息与历史块信息被重复计入。候选数轨迹检查（A2）**不会**暴露它——候选个数不变；它会在训练健康检查（损失偏高或发散）与训练动态剖面（幅度异常）中暴露，属于「最难查的一类 bug：结构对、语义错」，所以 4.3 的 B 类检查不可省略。

### 4.2 组件兼容性检查：三问分诊法

#### 4.2.1 概念说明

真实代码库里，残差流的周围站着很多组件。移植最大的隐性风险不是「改错行」，而是**目标库里有某个组件与双状态语义相互干扰**，而你以为它无关。本模块给出一套系统化的分诊方法：对目标库里的每个组件问三个问题。

**第一问：它是否读写子层之间的残差流？** 如果组件完全活在子层内部（RoPE 只旋转 q/k、GQA 只改头分组、SwiGLU 只改 MLP 内部激活、MoE 只是把 MLP 槽位换成路由加专家），那么它输出的仍是一个张量 `attn_out`/`mlp_out`，AttnRes 对它一无所知——**正交兼容**。如果组件直接挂在流上（PostNorm 在子层出口归一化整条流、GPT-J 式并行 attn+MLP 把两个位点合并成一个、层间 value residual 也在改聚合方式），就需要专门设计。

**第二问：它是否假设「流 = 历史之和」？** 标准残差下，进入第 \(l\) 层子层的流是 \(l\) 个子层输出之和，一些技术建立在这个解析性质上：按深度缩放分支输出的初始化（如按 \(1/\sqrt{l}\) 缩放残差分支）、对流做 soft-cap、依赖「流幅度随深度增长」的调试假设等。AttnRes 把流换成了凸组合（u1-l3：softmax 权重非负、和为 1，输出幅度被封顶在候选凸包内），**幅度统计特性完全改变**——这类假设必须逐条重推，不能照搬。

**第三问：它是否维护跨 token 的推理期状态？** 训练时全序列并行，一次前向结束状态即释放；自回归解码时 KV cache 跨步存活。AttnRes 在解码时需要**额外**为每个已封存块缓存其表示（深度注意力逐 token 作用，生成第 \(t{+}1\) 个 token 要读每个块在第 \(t\) 位置的那一列），这是「drop-in」唯一真正**泄漏到层 forward 之外**的地方，解码循环必须同步维护。

三问全过的组件，可以直接沿用；任何一问不过的，进入下表的对应处置。

#### 4.2.2 核心流程

分诊流程：

```text
对目标库的每个组件 C:
    Q1 C 是否读写子层之间的残差流?
        否 → Q2
        是 → 看「挂在流的哪一段」: 出口 Norm/并行结构 → 需重设计
    Q2 C 是否假设「流 = 历史之和」(幅度/缩放类假设)?
        否 → Q3
        是 → 该假设须按凸组合语义重推后重验证
    Q3 C 是否维护跨 token 的推理期状态?
        否 → 正交兼容, 放行
        是 → 解码路径需新增 blocks 缓存的维护 (N·d / token)
```

常见组件的对照表（结论依据：伪代码结构 + README 证据；涉及论文细节处标注待确认）：

| 组件 | 三问归类 | 结论 | 依据与备注 |
|:---|:---|:---|:---|
| RoPE / ALiBi 等位置编码（子层内部） | Q1 否 | 正交兼容 | 只旋转 q/k 或加偏置，不碰流；README 未给直接消融，待确认 |
| GQA / MQA | Q1 否 | 正交兼容 | 只改 KV 头分组 |
| 线性注意力（KDA 等） | Q1 否 | 正交兼容且有大规模证据 | [README.md:L105](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L105) 底座 Kimi Linear 即线性注意力混排 |
| MoE | Q1 否（占 MLP 槽位） | 正交兼容且有大规模证据 | 同上：Kimi Linear 48B 是 MoE，九项基准全面收益 |
| SwiGLU / GLU 激活 | Q1 否 | 正交兼容 | 只改 MLP 内部 |
| 学习式 / 正弦位置嵌入 | 嵌入层 | 兼容 | 进入第 0 层前已加和，随嵌入封存 `blocks[0]`（[README.md:L70](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L70)） |
| PreNorm 用 LayerNorm 还是 RMSNorm | 子层入口 | 兼容（u2-l4 实测 LayerNorm 版） | 与 `attn_res_norm` 是两回事（2.4 节） |
| dropout（子层内 / 嵌入后） | Q1 否 | 兼容 | 作用在子层输出或嵌入上，两模式同配即公平；eval 下自动关闭 |
| LayerScale / stochastic depth | 只改 \(v_l\) | 兼容 | 乘或丢弃的都是子层输出，不碰聚合语义 |
| **PostNorm** | Q1 是（出口 Norm 挂在流上） | **不在论文设定内** | [README.md:L37](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L37) 的问题陈述以 PreNorm 为背景；强行移植语义不同，须自行设计并验证，待确认 |
| **并行 attn+MLP（GPT-J 式）** | Q1 是（两位点合并） | 伪代码未覆盖 | 需重新设计位点与边界计数，待确认 |
| 按深度缩放分支的初始化 | Q2 是 | 假设失效，须重推 | 流不再是和，缩放依据变了 |
| **KV cache 自回归解码** | Q3 是 | 解码循环需维护 blocks 缓存 | 每步追加各块新列，开销 \(N \cdot d\)/token（4.2.3 末尾算账） |
| 梯度检查点 | 工程层 | 兼容 | 双状态作为分段输入/输出传递，重算语义不变 |
| `torch.compile` / CUDA graph | 工程层 | list 状态可能图断裂 | 可改为定长 \([N{+}1, B, T, D]\) 缓冲；待确认 |
| 权重绑定（`lm_head` = `wte`） | 输出端 | 兼容 | LM 头只换读取对象，权重共享关系不变 |

#### 4.2.3 源码精读

**兼容性的结构根基：子层是黑盒**。

> [README.md:L80](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L80)
> `attn_out = self.attn(self.attn_norm(h))`
>
> [README.md:L87](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L87)
> `mlp_out = self.mlp(self.mlp_norm(h))`

伪代码对子层的全部要求是「吃进 `norm(h)`、吐出一个同形张量」。RoPE、GQA、SwiGLU、MoE 全都发生在这个黑盒内部，AttnRes 既不知道也不关心——这就是「子层内部」类组件全部正交的形式化理由。

**大规模证据：MoE + 线性注意力底座上的全面收益**。

> [README.md:L105](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L105)
> `### Downstream Performance (Kimi Linear 48B / 3B activated, 1.4T tokens)`

u3-l3 的解读在这里变成移植者视角的证据：论文的实验底座本身就把 MLP 换成了 MoE、注意力混排了线性注意力变体——AttnRes 在**这些替换已经发生**的模型上仍然全面收益，说明它不依赖「朴素 MHA + 朴素 MLP」的假设。这是对照表里两条「有大规模证据」结论的出处。至于 RoPE 与 AttnRes 的直接组合，README 未单独报告，以论文消融为准（待确认；u3-l4 的精读框架可用来核对）。

**PostNorm 不在设定内的出处**。

> [README.md:L37](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L37)
> As depth grows, this uniform aggregation dilutes each layer's contribution and causes hidden-state magnitudes to grow unboundedly — a well-known problem with **PreNorm**.

问题陈述、动机（u1-l2 的膨胀与稀释）与解法（u1-l3 的凸组合封顶）全部围绕 PreNorm 的「裸主干」展开。PostNorm 的流在子层出口被归一化（幅度本就有界、但梯度捷径被切断，u1-l2 的对比），AttnRes 针对的问题在那里并不以同样形式存在。给 PostNorm 目标移植前，先想清楚你要解决什么——这不是本份 diff 能直接回答的问题。

**KV cache 场景的开销账**。解码时每个已封存块要缓存表示的已生成前缀，每生成一个 token 追加一列：

\[ \text{blocks 缓存} = N \cdot d \;\text{每 token}, \qquad \text{MHA 的 KV cache} = 2 L \cdot d \;\text{每 token}, \qquad \text{相对增量} = \frac{N}{2L} \]

以论文推荐 \(N \approx 8\)（[README.md:L47](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L47)）计：L=8 的迷你模型增量为 50%（不可忽略！），L=48 的深模型约 8%（边际）。GQA 底座分母更小、占比更高；线性注意力底座（Kimi Linear 系）的 KV 侧是定长递归状态，blocks 缓存的相对占比还会上升——论文附录 B 给出的两阶段推理 I/O 推导正是针对这类开销的对策方向（转引 u3-l4 的精读地图，细节以论文为准，待确认）。这呼应 u3-l1 的结论：**N 是计算与显存的旋钮，与深度解耦**（[README.md:L28](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L28) 的 O(Ld)→O(Nd)）。

#### 4.2.4 代码实践：给移植版加 RoPE，实证「正交」

1. **实践目标**：把 RoPE（子层内部组件的典型代表）加进 4.1.4 已移植好的模型注意力里，验证三问分诊的预言——结构不变式完全不受影响（参数增量仍是 \(4dL\)、候选数轨迹不变、单候选恒等），且训练保持健康。
2. **操作步骤**：

```python
# 示例代码：RoPE 注意力 + 三项验证（接 4.1.4 的定义）
def rope_cos_sin(T, hd, base=10000.0, device=None):
    inv = 1.0 / (base ** (torch.arange(0, hd, 2, device=device).float() / hd))
    t = torch.arange(T, device=device).float()
    f = torch.outer(t, inv)                          # [T, hd/2]
    return f.cos(), f.sin()

def apply_rope(x, cos, sin):                         # x: [B, H, T, hd]
    x1, x2 = x[..., ::2], x[..., 1::2]               # 相邻两维配对（NeoX 风格）
    cos, sin = cos[None, None], sin[None, None]      # 广播到批/头维
    return torch.stack([x1 * cos - x2 * sin,
                        x1 * sin + x2 * cos], dim=-1).flatten(-2)

class RoPEAttention(NanoAttention):                  # 只动子层内部
    def forward(self, x):
        B, T, D = x.shape
        q, k, v = self.c_attn(x).split(D, dim=2)
        q, k, v = (t.view(B, T, self.n_head, self.hd).transpose(1, 2)
                   for t in (q, k, v))
        cos, sin = rope_cos_sin(T, self.hd, device=x.device)
        q, k = apply_rope(q, cos, sin), apply_rope(k, cos, sin)
        att = (q @ k.transpose(-2, -1)) / self.hd ** 0.5
        mask = torch.triu(torch.ones(T, T, dtype=torch.bool,
                                     device=x.device), 1)
        y = F.softmax(att.masked_fill(mask, float('-inf')), -1) @ v
        return self.c_proj(y.transpose(1, 2).reshape(B, T, D))

# ---- 验证 1: 参数增量仍恰为 4dL（RoPE 无参数）----
torch.manual_seed(0)
port_rope = NanoGPTAR(V, d=D, n_head=4, n_layer=L, block_size=BS,
                      t_max=128, attn_cls=RoPEAttention)
print("rope 版参数增量:", cnt(port_rope) - cnt(orig), " 仍应 =", 4 * D * L)

# ---- 验证 2: 候选数轨迹与无 RoPE 版完全一致 ----
plain, trace = block_attn_res, []
def probe(blocks, partial_block, proj, norm):
    trace.append(len(blocks) + 1)
    return plain(blocks, partial_block, proj, norm)
block_attn_res = probe                     # 与 u2-l4 相同的探针手法
port_rope(torch.randint(0, V, (2, 64)))
block_attn_res = plain

k = BS // 2
expect = []
for l in range(L):
    expect += [(l + k - 1) // k + 1, l // k + 2]
print("候选数轨迹一致:", trace == expect)

# ---- 验证 3: 冒烟训练（复用 u2-l4 的 build_corpus/get_batch 与循环）----
# 用 tiny shakespeare 之类字符语料, train 200 步, lr=1e-3, 观察损失明显下降
```

3. **需要观察的现象**：rope 版参数增量与 4.1.4 的打印值完全相同（2048）；候选数轨迹为 `True`；200 步内训练损失从 ≈ \(\ln V\) 明显下降、无 NaN。
4. **预期结果**：三条全部通过——RoPE 只改 `NanoAttention.forward` 内部的 q/k，双状态结构对它无感知，这正是「Q1 否 → 放行」的实证。**注意**：本实践验证的是「兼容」（不互相破坏），**不是**「RoPE 与 AttnRes 组合效果更好」——后者是小规模实验回答不了的（u2-l4 的判读纪律），待本地验证也只报兼容性结论。

#### 4.2.5 小练习与答案

**练习 1**：用三问分诊法论证 GQA 与 AttnRes 的兼容性。

> **答案**：Q1：GQA 只改变 K/V 的头分组方式，发生在注意力内部，不读写子层之间的流——否；Q2：它不含任何关于流幅度的假设——否；Q3：它影响的是 KV cache 的大小（每 token \(2 L d_{kv}\)），但那是其自身的状态，与 blocks 缓存互不干扰——按 Q3 需注意解码侧要同时维护两种缓存，但无语义冲突。结论：正交兼容，仅解码循环的实现工作多一点。

**练习 2**：为什么 PostNorm 目标库**不能**直接套用本讲的 diff？

> **答案**：本份 diff 的每一步都建立在「子层读 `norm(h)`、输出裸加回 `partial`」的 PreNorm 布局上（L80/L87 的结构）。PostNorm 的布局是「裸流进子层、出口对求和结果归一化」，流在任何两层之间都已被归一化——幅度有界（u1-l2），README L37 陈述的问题在那里并不以同样形式存在，AttnRes 的动机与收益预期都要重新论证；且出口 Norm 挂在流上（Q1 是），封存进 blocks 的「块表示」语义也随之改变。属于「需重新设计」，不是本份 diff 的适用范围。

**练习 3**：某团队的模型用「残差分支输出按 \(1/\sqrt{l}\) 缩放」的初始化（\(l\) 为层号）。移植 AttnRes 后这个缩放出了问题，为什么？

> **答案**：该初始化假设进入第 \(l\) 层的流是 \(l\) 个子层输出之和，其幅度按 \(\sqrt{l}\) 增长，故分支输出要按 \(1/\sqrt{l}\) 压制以保持稳定（第二问命中）。AttnRes 把流换成了凸组合——幅度被候选凸包封顶、与深度无关（u1-l3），\(\sqrt{l}\) 的前提不成立，缩放依据必须重推。这是「假设流=和」类组件失效的典型例子。

### 4.3 上线前检查清单：四级防线与 preflight 自动化

#### 4.3.1 概念说明

移植完成后，「看起来能跑」与「可以开训」之间隔着一整份清单。清单的哲学来自前面各讲的教训：**确定性的检查先于统计性的结论**。结构不变式（A 类）是数学性质，秒级可验、结果非黑即白，任何一条失败都意味着实现 bug，训练再久也无意义；训练健康（B 类）几分钟短跑就能暴露绝大多数语义错误（比如 4.1.5 练习 3 那类「结构对、语义错」的 bug 恰好只能靠它抓）；统计纪律（C 类）决定下结论的资格；工程适配（D 类）决定这份代码能否真的走进训练集群与线上推理。

四级的顺序不可颠倒：A 不过不跑 B，B 不过不做 C，C 不过不谈「AttnRes 是否更好」。

#### 4.3.2 核心流程

**A 类：结构检查（确定性，必须全过，可全部自动化）**

| # | 检查项 | 方法 | 通过标准 |
|:---:|:---|:---|:---|
| A1 | 参数增量恰为 \(4dL\) | 移植版与原版参数量之差 | 严格相等（I5） |
| A2 | 候选数轨迹符合闭式 | 探针记录每个位点 `len(blocks)+1` | 与 I4 公式逐位相等 |
| A3 | 单候选退化为恒等 | `block_attn_res([], x, ...)` | 与 `x` 全等 |
| A4 | 状态与前向绑定 | 连续两次前向 | 第二次轨迹重新从 1 起步；同输入同输出 |
| A5 | `blocks[0]` 是完整嵌入 | 探针截获第 0 层位点 1 的 partial | 与 `wte(idx)+wpe(pos)` 逐元素相等 |

**B 类：训练健康检查（短跑即可发现）**

| # | 检查项 | 方法 | 通过标准 |
|:---:|:---|:---|:---|
| B1 | 初始损失 ≈ \(\ln V\) | 未训练模型一个随机批 | 两位小数内一致（u2-l4） |
| B2 | 单批过拟合 | 固定批反复训 300 步 | 损失降到远低于 1 |
| B3 | 无 NaN/Inf | 前 500 步巡检 | 全程有限 |
| B4 | 步耗时比 | 移植版/原版每步计时 | ≈ 1（FLOPs 增量约 1% 量级，u2-l3） |
| B5 | 显存形态 | 扫 L 看峰值显存增长 | 随块数 \(N{+}1\) 而非深度 L 增长（u3-l1） |

**C 类：统计纪律（下结论前的资格线）**——u2-l4 4.4 的全套在此逐条复用：`train()` 内部重设种子实现数据流配对；≥3 个种子报均值 ± std；\(\Delta\) 与 \(2\sigma\) 噪声底线比较后才谈方向；参数差与计算量差如实写进报告；小规模实验既不能证实也不能证伪 48B/1.4T tokens 的论文结论（[README.md:L105](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L105)，u3-l2/u3-l3）。

**D 类：工程适配（真上线前的最后一关）**

| # | 事项 | 要点 |
|:---:|:---|:---|
| D1 | 自回归解码路径 | 解码循环需同步维护 blocks 缓存（每步为各块追加新列，4.2.3 的开销账）；先 teacher-forcing 评测后自回归采样，两者都对才算通 |
| D2 | checkpoint 兼容 | 每层新增 4 个子模块随 `state_dict` 自动保存；检查加载/断点续训路径无遗漏、`use_attnres=False` 的旧权重可加载（多余键处理策略） |
| D3 | 优化器分组 | proj 权重是 2D 参数，多数框架默认会 weight decay；论文是否对其 decay 以论文超参为准（待确认）；norm 增益通常不 decay |
| D4 | `torch.compile`/CUDA graph | list 状态长度随深度变化可能触发图断裂；如需编译，考虑定长 \([N{+}1,B,T,D]\) 缓冲（待确认） |
| D5 | 分布式 | 双状态在层返回值中传递，DDP 无需 `find_unused_parameters`；梯度检查点把状态作为分段输入输出即可 |
| D6 | 回归测试 | 移植后跑一次 u2-l5 的幅度/梯度剖面：应呈现「幅度有界」的锯齿形态（[README.md:L121-L123](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L121-L123)）——它是比损失更灵敏的移植健康信号 |

#### 4.3.3 源码精读

**清单的「达标线」出处**——移植完成后你要对齐的就是这句承诺：

> [README.md:L47](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L47)
> With ~8 blocks, it recovers most of Full AttnRes's gains while serving as a practical drop-in replacement with marginal overhead.

「marginal overhead」可量化为 B4 的步耗时比 ≈ 1、参数增量 < 0.5%、显存随 \(N\) 而非 \(L\) 增长（B5）——三条 B 类检查就是这句话的验收单。

**统计纪律对照的原文**：

> [README.md:L99](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L99)
> AttnRes consistently outperforms the baseline across all compute budgets. Block AttnRes matches the loss of a baseline trained with **1.25x more compute**.

移植版的对比实验若要触碰这条结论，必须走完 C 类（且只以趋势为目标，u3-l2 的规模账：迷你台与论文差多个数量级）。

**健康回归的信号源**：

> [README.md:L121-L123](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L121-L123)
> AttnRes mitigates PreNorm dilution: output magnitudes remain bounded across depth and gradient norms distribute more uniformly across layers.

u2-l5 的 Probe 工具直接搬到移植版上：若移植正确，深度剖面应呈周期 \(k\) 的锯齿且包络不抬升；若幅度单调膨胀，说明聚合没真正生效（最常见原因：`use_attnres` 开关没打开、或位点被跳过）。

#### 4.3.4 代码实践：`preflight()` —— A 类检查全自动化

1. **实践目标**：把 A1–A5 与 B1 写成一个函数，对任意移植版模型一键输出 PASS/FAIL 表，作为每次改动后的第一反应动作。
2. **操作步骤**：

```python
# 示例代码：上线前结构检查自动化（接 4.1.4 的定义；探针手法同 u2-l4）
import math

@torch.no_grad()
def preflight(vocab, d, n_head, n_layer, block_size, t_max=128, T=64, B=2):
    model = NanoGPTAR(vocab, d=d, n_head=n_head, n_layer=n_layer,
                      block_size=block_size, t_max=t_max)
    model.eval()
    orig = NanoGPT(vocab, d=d, n_head=n_head, n_layer=n_layer, t_max=t_max)
    res = {}

    res['A1 参数增量==4dL'] = \
        (cnt(model) - cnt(orig)) == 4 * d * n_layer          # A1

    plain, trace = block_attn_res, []                        # A2+A5 探针
    def probe(blocks, partial_block, proj, norm):
        trace.append((len(blocks) + 1, partial_block))
        return plain(blocks, partial_block, proj, norm)
    globals()['block_attn_res'] = probe
    idx = torch.randint(0, vocab, (B, T))
    model(idx)
    globals()['block_attn_res'] = plain

    k = block_size // 2
    expect = []
    for l in range(n_layer):
        expect += [(l + k - 1) // k + 1, l // k + 2]
    res['A2 候选数轨迹'] = [n for n, _ in trace] == expect    # A2

    proj1, norm1 = nn.Linear(d, 1, bias=False), RMSNorm(d)   # A3
    x = torch.randn(2, 5, d)
    res['A3 单候选恒等'] = bool(torch.allclose(plain([], x, proj1, norm1), x))

    trace.clear()                                            # A4
    model(idx)
    res['A4 状态无残留'] = [n for n, _ in trace] == expect

    emb = model.wte(idx) + model.wpe(torch.arange(T))        # A5
    res['A5 嵌入入块'] = bool(torch.equal(trace[0][1], emb))

    tgt = torch.randint(0, vocab, (B, T))                    # B1
    _, loss = model(idx, tgt)
    res['B1 初始损失≈lnV'] = abs(loss.item() - math.log(vocab)) < 0.1

    for name, ok in res.items():
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    return res

preflight(vocab=65, d=64, n_head=4, n_layer=8, block_size=4)
```

3. **需要观察的现象**：六行输出全部 `PASS`。
4. **预期结果**：A1–A5 是结构决定的确定性检查，应全部严格通过（A3 是精确恒等：单元素 softmax 恒为 1）；B1 通过阈值 0.1 放宽（LayerNorm 版随机网络与近均匀假设的偏差略大于 u2-l4 的 RMSNorm 版）。若 A5 失败，说明外壳在嵌入与第 0 层之间还做过别的变换（例如嵌入 dropout——eval 下应关闭后再测）。B1 数值待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 A 类检查必须全部通过才允许启动训练？

> **答案**：A 类是结构不变式——由代码结构决定、不依赖数据与运气，验证成本秒级；任何一条 FAIL 都意味着实现与伪代码的结构偏离，后续所有训练结果（包括「AttnRes 更好/更差」）都无法归因。用秒级检查挡住小时级的无效训练，是清单第一级的意义；A3 这条恒等检查尤其便宜，却能在 30 秒内证实 `block_attn_res` 的语义正确。

**练习 2**：B4 步耗时比测出来是 1.5×，远超「约 1% 量级」的预期。给出三个排查方向。

> **答案**：① `block_size` 配置过小（如 2）使 Block 退化为 Full，候选数随深度线性增长、开销远超预期（u3-l1 的复杂度账，用 A2 轨迹末端候选数即可确诊）；② `torch.stack` 与两次 `einsum` 在每个位点重复执行，若目标库用 pipeline/编译优化过原路径而 attn_res 分支未被覆盖，profile 定位；③ 无意间保留了不必要的计算图引用（例如把中间张量存进了长生命周期的列表），使显存与耗时同步膨胀（对照 B5 的显存曲线）。

**练习 3**：训练已收敛且 B 类全过，为什么 D1 仍要求单独测自回归生成路径？

> **答案**：训练是 teacher forcing：全序列并行、一次前向释放全部状态；自回归解码是逐 token 增量，blocks 缓存的跨步维护是**新写的代码路径**（4.2 第三问），训练过程完全不会执行它。训练收敛只覆盖了前者的正确性；后者若有 bug（漏追加新列、缓存错位、位置索引偏移），症状是「评测正常、生成乱码」，必须专门测：先 teacher-forcing 评测，再自回归采样生成文本，两者都正常才算移植完成。

## 5. 综合实践

### 5.1 任务：把 Block AttnRes 移植进一个开源迷你 GPT

选取一份开源迷你 GPT 实现（nanoGPT 风格；直接克隆 nanoGPT，或沿用 4.1.4 的示例版均可，但**必须是「先有原版、再做移植」的顺序**——本讲练的就是移植），将 Block AttnRes 移植进去：嵌入层入 `blocks`、块边界调度、两组 proj/norm，保持与原模型参数量基本对齐，训练至收敛，并记录移植步骤与踩坑笔记。任务成功标准是**移植正确 + 训练收敛 + 记录完备**，「attnres 跑赢基线」不是成功标准——小规模下 \(\Delta\) 落入噪声是合法结果（u2-l4 4.4）。

产出物清单：

- `model_ar.diff` 或逐条改动的清单（对照 4.1.2 的对应表）；
- `preflight` 输出（A 类六项全 PASS 的截图/日志）；
- 训练/验证损失曲线（移植版 vs 原版，同种子配对，≥2 种子更佳）；
- 一份移植笔记（5.4 模板），含踩坑记录。

### 5.2 移植协议操作单（对照执行）

七步协议（4.1.2）的每一步都配一个「立即验证」，**不要攒到最后一起测**：

| 步 | 改动 | 该步完成后的即时验证 |
|:---:|:---|:---|
| S1 | 通读目标库，定位残差流两行与外壳循环 | 能在纸上画出「嵌入 → 位点1 → attn → 位点2 → mlp → … → ln_f → head」数据流 |
| S2 | 改层签名与外壳双状态搬运，加 `use_attnres` 开关 | 开关关闭时行为与原版一致：初始损失 ≈ \(\ln V\)、能正常训练下降 |
| S3 | 每层新增两组 proj/norm | **A1**：参数增量恰为 \(4dL\) |
| S4 | 接位点 1 + 边界封存 | **A2 前半**：前若干位点候选数符合闭式 |
| S5 | 接位点 2 | **A2 全程**：整条轨迹符合闭式 |
| S6 | 末端接线核对 | **A5**：`blocks[0]` 逐元素等于嵌入；末端读最后的 partial |
| S7 | 全量检查 + 冒烟训练 | `preflight()` 六项全 PASS；**B1–B3** 通过；200 步损失明显下降 |

### 5.3 常见踩坑速查表

| 症状 | 最可能原因 | 定位与修复 |
|:---|:---|:---|
| A2 轨迹整体偏移一层 | `layer_number` 从 1 起计数 | 改 0 起点（I2、u2-l2） |
| A2 候选数增长过快 | `block_size` 忘了 `//2`（按层而非子层计数） | 对齐 [README.md:L74](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L74) 注释口径 |
| 训练发散 / 损失居高不下 | 把聚合 `h` 写回 `partial`，破坏 write-once 语义 | 核对并回行只能是 `partial + 子层输出`（[README.md:L81](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L81)、[README.md:L88](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L88)）；见 4.1.5 练习 3 |
| 第二个 batch 起形状报错 | `blocks` 未在每次前向开始时重置（如挪进了 `__init__`） | 外壳 `blocks = []`（I3） |
| 显存随深度线性暴涨 | `block_size=2` 退化 Full，或保留了过时的张量引用 | u3-l1 复杂度账 + B5 显存扫描 |
| 训练收敛但生成乱码 | 解码循环未维护 blocks 缓存 | D1：teacher-forcing 与自回归分别验证 |
| `torch.compile` 报 graph break | list 状态长度动态变化 | 定长缓冲或先关闭编译（D4） |
| 步耗时比 ~1.5× | 候选过碎 / attn_res 分支未优化 / 计算图残留 | 4.3.5 练习 2 的三个方向 |
| 幅度剖面单调膨胀（应呈锯齿） | `use_attnres` 未生效或位点被跳过 | u2-l5 的 Probe 回归测试（D6） |

### 5.4 移植笔记模板

```markdown
# Block AttnRes 移植笔记：<目标库名@版本/commit>
## 1. 目标库画像
   残差流写法（原代码摘抄 3-5 行）/ 层数 L、宽度 d / 在场组件清单
   （RoPE? MoE? dropout? 检查点? compile? → 各过一遍三问分诊）
## 2. 改动清单（对照 4.1.2 对应表逐条记录）
   层签名 / 位点 1 / 边界 / 位点 2 / 外壳 / 新参数 / 新超参 block_size=?
## 3. preflight 结果（六项逐条粘贴）
## 4. 训练记录
   数据 / steps / lr / 种子；原版与移植版参数量与相对差（应为 4dL）
   final val（均值 ± std）；Δ 与 2σ 的比较结论
## 5. 生成路径验证（D1）
   teacher-forcing ppl / 自回归采样样例（各 3-5 行）
## 6. 踩坑记录（对照 5.3，写症状 → 定位过程 → 修复）
## 7. 兼容性结论表（目标库每个组件一行：正交 / 已适配 / 待确认）
```

### 5.5 判读纪律

沿用 u2-l4 4.4 与 u3-l2 的全套纪律，此处只列关键三条：**配对种子**（`train()` 内部重设，两版本看到相同批次序列）；**\(\Delta\) 与 \(2\sigma\) 比较后再谈方向**，落入噪声就写「本规模下无显著差异」；**不外推**——README 的收益证据取自 48B/1.4T tokens（[README.md:L105](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L105)），迷你规模只能验证「移植正确、训练正常、趋势不矛盾」。所有数值待本地验证。

## 6. 本讲小结

- **集成改造点**：移植契约 = 三类改动（层签名双状态、两个 attn_res 位点、外壳 `blocks` 重置与双状态搬运）+ 五个零改动（子层内部、入口 Norm、嵌入与损失、数据管线、评测代码）；层内净增约 10 行、参数增量恰为 \(4dL\)、唯一新超参 `block_size`——[README.md:L33](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L33)「drop-in」在 diff 尺度上的样子；建议保留 `use_attnres` 开关让原版形态共存。
- **组件兼容性检查**：三问分诊——是否读写子层间的流、是否假设「流=历史之和」、是否维护跨 token 推理状态；子层内部组件（RoPE/GQA/线性注意力/MoE/SwiGLU）全部正交（[README.md:L80](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L80)/[L87](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L87) 的黑盒契约 + [L105](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L105) 的 MoE/线性注意力大规模证据）；PostNorm 与并行结构不在论文设定内；KV cache 是唯一泄漏到层外的改造点，每 token 开销 \(N \cdot d\)、相对 MHA 为 \(N/2L\)。
- **上线前检查清单**：四级不可颠倒——A 结构（参数增量、候选轨迹、单候选恒等、状态绑定、嵌入入块，全自动化于 `preflight()`）→ B 训练健康（初始损失、过拟合、NaN、耗时比、显存形态）→ C 统计纪律（配对种子、\(2\sigma\) 底线、不外推）→ D 工程适配（解码路径、checkpoint、优化器分组、编译、分布式、幅度剖面回归）。
- **本讲贯穿的证据链**：契约出自 L33、子层黑盒出自 L80/L87、配置出自 L47/L70/L74-L77、组件证据出自 L105、健康信号出自 L121-L123——一份 README 撑起一次完整移植的规格说明，这正是「论文发布仓库 + 伪代码蓝图」的正确打开方式。

## 7. 下一步学习建议

- **手册收官后的第一件事**：把本讲的移植产物固化成模板——`preflight()`、七步操作单、移植笔记模板合起来，就是你团队内部的「AttnRes 移植工具包」；下次面对新代码库，从 5.2 的 S1 直接开工。
- **补齐推理侧的细节**：训练侧的移植本讲已闭环，推理侧还有一块硬骨头——解码循环的 blocks 缓存实现与论文附录 B 的两阶段推理 I/O 推导（转引 u3-l4 的精读地图，细节以论文为准，待确认）。给你的移植版实现一个带 blocks 缓存的 `generate()`，是检验 D1 的最终作业。
- **把谱系读宽**：AttnRes 不是唯一的跨层聚合方案。沿 u3-l4 提到的相关工作谱系（DenseFormer、mHC、MUDDFormer、MRLA 等三族方法）做一轮对比精读，重点看它们与 AttnRes 在「权重是否输入依赖」「候选粒度」「显存策略」上的差异——你会发现本讲的三问分诊法对它们同样适用。
- **向真实规模靠近**：若条件允许，把移植版从字符级迷你台升级到一个小型 BPE 级模型（数千万参数），重跑 u3-l2 的多规模扫描与 u3-l3 的下游评测雏形——那是「趋势复现」能触及的上一级台阶，也是向论文结论靠近的最后一公里。
- **回望全链路**：u1 的问题与公式 → u2 的逐行精读与实验台 → u3 的开销、scaling、评测、精读与本讲的移植——13 讲走完，你已经把一份「只有 README 的仓库」变成了自己手里一套可复现、可迁移、可扩展的方法。保持这个习惯：**读论文仓库时，永远追问「伪代码能不能变成我的代码」**。

