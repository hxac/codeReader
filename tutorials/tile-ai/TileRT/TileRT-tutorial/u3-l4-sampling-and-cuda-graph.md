# 采样、CUDA Graph 重捕获与 logprobs 导出

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 TileRT 把采样参数（temperature / top_p / top_k / use_topp）和请求级随机种子分别存放在哪两个 `temp_vars` 槽里，以及为什么这两者的「改动代价」完全不同。
- 读懂 `update_sampling_config` 的判等短路逻辑：参数没变就直接 `return`，参数变了必须先 `go_home` 释放旧 CUDA Graph、改写 `SAMPLING_CONFIG` 槽、再 `prepare_money` 重新捕获图。
- 理解 `set_sampling_seed` 为什么「按请求设置一次、不需要重捕图」，以及它和 `SAMPLING_POSITIONS` 配合产生每步随机性的设计。
- 掌握 logprobs 导出的「开关式」实现：`LOGPROBS_FLAG` 是一个普通张量，置 1/0 即可开关 top-256 导出，全程不触发图重捕获。

本讲承接 u2-3（`ShowHandsDSALayer` 与 `prepare_money` 绑定契约）和 u3-2（非 MTP 解码主循环读写 `temp_vars` 各槽），把视角收束到「采样这一环」上。

## 2. 前置知识

### 2.1 采样的两种模式

给定最后一层的 logits \(z\in\mathbb{R}^{V}\)（\(V\) 为词表大小），采样决定下一个 token：

- **top-1（argmax / 贪心）**：直接取最大 logit 对应的 token，无随机性。TileRT 里用 `use_topp=False` 表示。
- **top-p（核采样 / nucleus）**：先按温度缩放概率 \(p_i=\mathrm{softmax}(z_i/T)\)，再把 token 按概率从大到小排序，取累计概率首次达到 \(p\) 的最小集合，在该集合内重新归一化后采样。TileRT 里用 `use_topp=True` 表示，并用 `top_k=256` 限定候选集上界。

行内公式写作 \(p_i=\mathrm{softmax}(z_i/T)\)，独立公式写作：

\[
S_p = \min\left\{S \;\middle|\; \sum_{i\in S} p_i \ge p,\; |S|\le \text{top\_k}\right\}
\]

### 2.2 CUDA Graph 与「固化」

CUDA Graph 把一连串 kernel 启动录制成一张可整体回放的图，省掉每步的 CPU 启动开销——这对 TileRT 这种把单 token 延迟（TPOT）压到毫秒级的系统至关重要。但「录制」意味着图里**写死了要启动哪些 kernel、用什么 launch 配置、读哪些张量地址**。这正是 TileRT 用牌桌隐喻命名控制算子的原因：

| 算子 | 牌桌隐喻 | 实际动作 |
|------|----------|----------|
| `dsa_show_hands_prepare_money` | 「准备赌本」 | 绑定四元张量 + **捕获 CUDA Graph** |
| `dsa_show_hands` | 「亮牌」 | 回放图，跑一步前向 |
| `dsa_show_hands_go_home` | 「回家」 | **释放已捕获的图** |
| `dsa_show_hands_reset` | 「重新发牌」 | 清 KV 缓存，不动图 |

核心直觉：**采样「模式」决定图里录哪些 kernel，所以换模式要重捕图；采样的「数值」与「种子」只是图回放时读取的张量内容，改它们不用重捕图。** 本讲就是围绕这一直觉展开。

### 2.3 `Idx` 扁平下标（承接 u2-5）

后端不认识名字，只认识一个扁平张量列表 `temp_vars`。`Idx`（`DsaTempVarIdx`）这个 `IntEnum` 给下标起名：`temp_vars[Idx.SAMPLING_CONFIG]` 等价于 `temp_vars[48]`。本讲会频繁用到下面几个槽。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `tilert/models/deepseek_v3_2/temp_var_indices.py` | 定义采样相关槽位的下标枚举 `SAMPLING_SEED / SAMPLING_POSITIONS / SAMPLING_CONFIG / TOP_P_SCORES / TOP_N_LOG_PROBS / TOP_N_INDICES / LOGPROBS_FLAG` |
| `tilert/models/deepseek_v3_2/modules/dsa.py` | `get_temp_vars` 给上述槽位分配具体 shape 与 dtype |
| `tilert/models/deepseek_v3_2/modules/end2end.py` | `update_sampling_config`（重捕图）、`set_sampling_seed`（设种子）、logprobs 的三个 getter/setter，以及 `prepare_money / go_home` 的算子包装 |
| `tilert/models/deepseek_v3_2/generator.py` | 对外 API `update_sampling_params`、每次 `generate` 开头调 `set_sampling_seed` |
| `tilert/generate.py` | CLI 参数 `--sampling-seed`、`--enable-logprobs` |
| `tilert/benchmark/__init__.py` | `apply_mode` 在基准模式间切换采样参数，是触发重捕的真实场景 |

> 说明：本讲以 DeepSeek-V3.2 路径为主线。GLM-5 路径（`glm_5/modules/end2end.py`、`glm_5/generator.py`）是镜像实现，逻辑一致，只是算子名多带 `_glm5` 后缀。

## 4. 核心概念与源码讲解

### 4.1 采样的运行时表示：`SAMPLING_CONFIG` 槽与请求级种子

#### 4.1.1 概念说明

TileRT 的采样由两部分组成：

1. **采样配置**（决定「怎么采」）：温度、top_p、top_k、是否用 top-p。这四元组被打包进**一个 4 元 FP32 张量** `SAMPLING_CONFIG`，每个卡各一份。
2. **随机种子**（决定「采到哪个」）：一个请求级整数，整个请求期间不变，但每一步解码靠「位置」产生不同的随机偏移，从而得到不同的采样结果。

为什么要分开？因为这两者的「改动代价」天差地别：改配置可能改变图里录的 kernel（详见 4.2），必须重捕图；改种子只是写一个张量，图照常回放。把它们放进不同的槽、用不同的 API 触发，是这个设计的精髓。

#### 4.1.2 核心流程

采样相关槽位在 `Idx` 枚举里是一段连续区域：

[tilert/models/deepseek_v3_2/temp_var_indices.py:L64-L73](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/temp_var_indices.py#L64-L73) — 定义 7 个采样相关槽的下标，关键是 `SAMPLING_CONFIG=48`（4 元配置）和 `SAMPLING_SEED=46`、`SAMPLING_POSITIONS=47`（种子与位置）。

这些槽的实际 shape/dtype 在 `Dsa.get_temp_vars` 里分配：

[tilert/models/deepseek_v3_2/modules/dsa.py:L209-L223](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/dsa.py#L209-L223) — 给采样相关槽分配张量，含义见下表。

| 槽位 | dtype | shape | 含义 |
|------|-------|-------|------|
| `SAMPLING_SEED` (46) | int64 | `[bs, seq]` | 请求级种子（整个请求固定） |
| `SAMPLING_POSITIONS` (47) | int64 | `[bs, seq]` | 每步的位置，提供「每步随机性」 |
| `SAMPLING_CONFIG` (48) | float32 | `[4]` | `(temperature, top_p, top_k, use_topp)` 四元组 |
| `TOP_P_SCORES` (49) | float32 | `[bs, seq]` | 采样到的那个 token 的 log-prob（单值） |
| `TOP_N_LOG_PROBS` (53) | float32 | `[bs, seq, 256]` | top-256 候选的 log-prob |
| `TOP_N_INDICES` (54) | int32 | `[bs, seq, 256]` | top-256 候选的 token id |
| `LOGPROBS_FLAG` (55) | int32 | `[1]` | logprobs 导出开关（1 开 / 0 关） |

注意 `SAMPLING_CONFIG` 的构造：它直接由 `extra_args` 里的四个值打包：

```python
temp_vars[Idx.SAMPLING_CONFIG] = torch.tensor(
    [temperature, top_p, top_k, use_topp], **fp32_desc
)
```

详见 [tilert/models/deepseek_v3_2/modules/dsa.py:L211-L213](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/dsa.py#L211-L213)。这里 `use_topp` 是 Python `bool`，被 `torch.tensor(..., dtype=float32)` 隐式转成 `1.0`/`0.0`；`top_k`（如 256）也被存成浮点 `256.0`，由后端 kernel 自己解释。

#### 4.1.3 一个隐蔽的细节：连续存储会清零

这里有一个承接 u2-l5 的关键点，初读很容易错过。`get_temp_vars` 给 `SAMPLING_CONFIG` 赋了初值，但 `ShowHandsDSALayer` 在加载权重时会把全部 `temp_vars` 喂给 `generate_params_with_continuous_storage`，而后者**用 `torch.zeros` 建一块大显存再做视图**——也就是说视图里的值全是 0，`get_temp_vars` 里设的初值被丢弃了：

[tilert/models/deepseek_v3_2/modules/end2end.py:L402-L416](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L402-L416) — `get_temp_vars(...)` 的返回值被 `generate_params_with_continuous_storage` 包成连续存储视图。

因此紧接着必须**手动把 `SAMPLING_CONFIG` 写回新视图**：

[tilert/models/deepseek_v3_2/modules/end2end.py:L418-L430](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L418-L430) — 用 `.copy_()` 把四元组写进连续存储视图里的 `SAMPLING_CONFIG` 槽。

```python
sampling_config = intermediates[Idx.SAMPLING_CONFIG]
sampling_config.copy_(
    torch.tensor(
        [self.temperature, self.top_p, float(self.top_k),
         1.0 if self.use_topp else 0.0],
        dtype=torch.float32, device=device_id,
    )
)
```

这一步也解释了为什么 `SAMPLING_CONFIG` 必须是「可写的普通张量」而不是常量：它的值要在不重建整张图的前提下被覆写。

#### 4.1.4 请求级种子：`set_sampling_seed`

种子走的是完全不同的通道——一个独立的 C++ 算子，不碰 `SAMPLING_CONFIG`，也不触发图重捕获：

[tilert/models/deepseek_v3_2/modules/end2end.py:L123-L134](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L123-L134) — `dsa_show_hands_set_sampling_seed` 包装算子 `dsa_show_hands_set_sampling_seed`。

它的 docstring 点明了设计意图：**「种子整个请求固定，位置提供每步变化」**（"The seed is fixed for the entire request. Position provides per-step variation."）。也就是说，同一个 seed 配合 `SAMPLING_POSITIONS` 里随步推进的位置，既保证「同一请求可复现」，又保证「不同步骤产出不同 token」。

[tilert/models/deepseek_v3_2/modules/end2end.py:L560-L570](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L560-L570) — `ShowHandsDSALayer.set_sampling_seed` 方法，转发给上面那个算子。

在生成器层面，**每次 `generate()` 开头都调一次设种子**，用构造时记下的 `self.sampling_seed`：

[tilert/models/deepseek_v3_2/generator.py:L176-L185](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/generator.py#L176-L185) — `generate` 在分支前先 `set_sampling_seed`。

这就形成了「一请求一种子」的语义：想要可复现就给同一个 `sampling_seed`，想要每次不同就换种子——但**换种子绝不重捕图**。

#### 4.1.5 代码实践

**实践目标**：确认 `SAMPLING_CONFIG` 的布局与「连续存储清零后需 copy_ 写回」这一隐蔽行为。

**操作步骤**（源码阅读型，无需 GPU）：

1. 读 `dsa.py` 的 `get_temp_vars`，列出 7 个采样槽的 dtype/shape（见 4.1.2 表格）。
2. 读 `end2end.py` 第 402–430 行，对照 `generate_params_with_continuous_storage`（第 321–334 行）确认：被包进连续存储的张量是新分配的零值视图，而非原张量的拷贝。
3. 回答：若删掉第 418–430 行的 `.copy_()`，首步采样会用什么 `temperature/top_p`？为什么？

**预期结果**：删掉后 `SAMPLING_CONFIG` 全为 0——`temperature=0` 会让 softmax 退化（logits/0），`top_p=0` 会让核采样集合为空。这会立刻暴露为采样错误，所以这段 `copy_` 是不可或缺的。

**待本地验证**：在真实 8 卡环境删改后运行一次 `generate`，观察是否在首次采样处报错或产出异常 token。

#### 4.1.6 小练习与答案

**练习 1**：为什么 `SAMPLING_SEED` 用 int64 而 `SAMPLING_CONFIG` 用 float32？
**参考答案**：种子是用于驱动伪随机数生成器（PRNG）的整数状态，int64 能容纳完整 64 位种子空间；而 `temperature/top_p/top_k/use_topp` 都是连续或可表为浮点的配置值，后端 kernel 用浮点比较和算术处理它们，故存 float32。

**练习 2**：两次连续调用 `generate(prompt)`，若 `sampling_seed` 不变、`enable_thinking` 不变、采样配置不变，产出是否一定相同？
**参考答案**：在「同种子 + 同位置序列」下，采样是确定性的，故产出应当相同（这是可复现性的来源）。注意：位置由解码步推进决定，配置不变时步序列相同，因此可复现。

---

### 4.2 CUDA Graph 固化与重捕获：`prepare_money` / `go_home` 与 `update_sampling_config`

#### 4.2.1 概念说明

CUDA Graph 在 `prepare_money` 时被捕获：它把「这步解码要跑的全部 kernel」录制成图。问题是——采样的「模式」会改变要跑哪些 kernel：

- `use_topp=False`（top-1）走的是 argmax 路径；
- `use_topp=True`（top-p）走的是排序 + 核采样 kernel，且 `top_k` 会影响该 kernel 的 launch 几何。

这些「选哪个 kernel、用什么 launch 配置」是**录制时**就定死的，回放时不能改。所以一旦采样模式变了，旧图就作废，必须 `go_home`（释放旧图）再 `prepare_money`（重新捕获新图）。

TileRT 不去精细判断「到底哪个参数影响 kernel」，而是**保守地把整个四元组 `(temperature, top_p, top_k, use_topp)` 当作图的身份证**：只要它变了就重捕。这样实现简单、不易错——代价是改一个不影响 kernel 的纯数值（比如只调 temperature）也会触发一次重捕，但重捕是一次性开销，发生在请求切换的间隙，对稳态 TPOT 无影响。

#### 4.2.2 核心流程：`update_sampling_config`

[tilert/models/deepseek_v3_2/modules/end2end.py:L253-L311](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L253-L311) — `update_sampling_config` 全函数，是本讲的核心。

它的执行过程可以拆成五段：

```text
1. 算 new_config 与 current_config 两个四元组
2. 若相等 → 直接 return（短路，零开销）   ← 关键优化
3. go_home 释放旧图（MTP 模式放两张图，故调两次）
4. 更新 self.temperature/top_p/top_k/use_topp
5. 逐卡把新四元组 copy_ 进 SAMPLING_CONFIG 槽
6. 逐卡 prepare_money 重捕图（MTP 模式调两次：完整图 + 主模型子图）
```

逐段对应源码：

**第 1、2 步——判等短路**（第 257–260 行）：

```python
new_config = (temperature, top_p, top_k, use_topp)
current_config = (self.temperature, self.top_p, self.top_k, self.use_topp)
if new_config == current_config:
    return
```

这是「为何 `new_config==current_config` 时直接 return」的直接答案：**没变就不必付重捕图的昂贵代价**。基准套件在多个模式间循环时，相邻两次若恰好同配置，就靠这一行省下一次 `prepare_money`。

**第 3 步——`go_home` 释放旧图**（第 267–271 行）：

[tilert/models/deepseek_v3_2/modules/end2end.py:L115-L120](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L115-L120) — `dsa_show_hands_go_home` 包装算子。

```python
if self.with_mtp:
    dsa_show_hands_go_home(True, self.is_glm5)   # 释放 MTP 完整图
    dsa_show_hands_go_home(False, self.is_glm5)  # 释放主模型子图
else:
    dsa_show_hands_go_home(False, self.is_glm5)
```

为什么 MTP 要调两次？因为 u2-3 讲过，MTP 模式下 `prepare_money` 本就被调两遍——一遍捕获「主模型 + MTP 头」的完整图，一遍只捕获「主模型」子图（供 prefill 阶段只用主模型时回放）。释放与重捕必须成对：捕了几张就放几张。

**第 5 步——改写配置槽**（第 278–288 行）：逐卡把新四元组 `.copy_()` 进 `SAMPLING_CONFIG`，与 4.1.3 里 `_init_weights` 的写法完全一致。注意顺序：**先改配置，后重捕图**，这样捕获进图的「初始配置」就是新值。

**第 6 步——`prepare_money` 重捕**（第 290–311 行）：

[tilert/models/deepseek_v3_2/modules/end2end.py:L79-L96](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L79-L96) — `dsa_show_hands_prepare_money` 包装算子，按 `with_mtp` / `is_glm5` 拼出正确的算子名。

非 MTP 模式只调一次；MTP 模式调两次（第二次用 `params[:base_params_count]`、`caches[:base_caches_count]` 只绑主模型部分，对应主模型子图）。

#### 4.2.3 对外入口：`update_sampling_params` 与 `apply_mode`

用户不直接调 `update_sampling_config`，而是调生成器的 `update_sampling_params`：

[tilert/models/deepseek_v3_2/generator.py:L138-L152](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/generator.py#L138-L152) — 先更新 `self` 字段（供下次 `generate` 的种子默认值等），再委托 `decode_layer.update_sampling_config`。

它同时干两件事：更新生成器自己记的字段 + 触发图重捕获。基准套件的 `apply_mode` 正是它的真实调用方：

[tilert/benchmark/__init__.py:L47-L54](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/benchmark/__init__.py#L47-L54) — `apply_mode` 把一个 `BenchMode`（如「top-k1 w/o MTP」「top-p0.95 w/ MTP」）的采样参数下发，模式之间一旦 `use_topp` 或 `top_p` 不同，就会在这里触发一次图重捕获。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：亲手验证「判等短路」与「换参数必先 `go_home` 再 `prepare_money`」的设计，并量化重捕获对首 token 延迟的影响。

**操作步骤**：

1. **读源码回答原理题**：对照 `update_sampling_config`（第 253–311 行）解释——
   - 为何 `new_config == current_config` 时直接 `return`？答：四元组是图的「身份证」，没变就不必付重捕代价。
   - 为何不相等时必须先 `go_home` 再 `prepare_money`？答：旧图已把旧 kernel 选择/launch 配置固化，不释放直接重捕会泄漏显存并混淆图状态；`go_home` 先释放，`prepare_money` 再以新配置捕获。
2. **设计对照实验**（需要 8 卡 B200 环境，否则标注「待本地验证」）：
   - 准备两个脚本：
     - **A 固定 top_p**：构造生成器时 `use_topp=True, top_p=0.9`，连续 `generate` 10 次，不调 `update_sampling_params`。
     - **B 每次换 top_p**：每次 `generate` 前调 `update_sampling_params(top_p=交替的 0.9/0.95)`。
   - 两个脚本都用相同的 `max_new_tokens` 和 prompt，记录**每次请求的首 token 延迟**（即 `time_list[0]`）。
3. **需要观察的现象**：脚本 B 中，每次切换 top_p 都会在日志看到 `Recapturing CUDA graphs: temperature=..., top_p=..., top_k=..., use_topp=...`（第 262–265 行的 `print`），且首 token 延迟显著高于脚本 A 的稳态延迟；脚本 A 则没有这条日志，首 token 延迟接近稳态。
4. **预期结果**：脚本 B 的首 token 延迟包含一次 `prepare_money` 的捕获开销（通常远大于单步解码），脚本 A 则无此开销。这正好印证「判等短路」的价值——固定配置时图只捕一次。
5. **若无法在 GPU 上运行**：明确写「待本地验证」，并改为源码阅读型——在 `update_sampling_config` 第 262 行的 `print` 处加注释，说明这条日志何时出现、出现意味着什么。

#### 4.2.5 小练习与答案

**练习 1**：如果只想把 `temperature` 从 1.0 调到 0.8（其他不变），`update_sampling_config` 会重捕图吗？这合理吗？
**参考答案**：会，因为四元组变了。这是「保守判等」的代价：实现上无法（也不必）判断 temperature 是否影响 kernel 选择，索性任何变化都重捕。由于这只发生在请求切换间隙、对稳态 TPOT 无影响，是合理的工程取舍。

**练习 2**：在 MTP 模式下，`update_sampling_config` 里 `go_home` 与 `prepare_money` 各调几次？为什么必须相等？
**参考答案**：各调两次（一次对应「主模型 + MTP」完整图，一次对应「仅主模型」子图）。因为 u2-3 里 `prepare_money` 在 MTP 模式本就捕了两张图，释放必须成对——漏放一张会泄漏显存，漏捕一张会让对应回放路径没有图可放。

---

### 4.3 logprobs 导出：top-256 开关与只读回读

#### 4.3.1 概念说明

有时（例如评测、蒸馏、调试）你想拿到每步采样分布的 top-256 候选及其 log-prob，而不只是最终采样到的那个 token。TileRT 的做法很轻量：top-p kernel **本来就在内部排序 top 候选**，只要给它一个「请把结果写出来」的开关，它就把 top-256 顺带写进两个槽 `TOP_N_LOG_PROBS` / `TOP_N_INDICES`。

关键在于这个开关 `LOGPROBS_FLAG` 是**普通 temp_var 张量**，置 1/0 即可——**完全不需要重捕图**。原因是它不改变录哪个 kernel，只改变 kernel 回放时「是否多写两块输出」，属于运行时读取的张量内容，不是图的固化结构。

#### 4.3.2 核心流程

**槽位分配**（top-256 的「256」写死在 `get_temp_vars`）：

[tilert/models/deepseek_v3_2/modules/dsa.py:L220-L223](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/dsa.py#L220-L223) — `max_top_n = 256`，分配 `TOP_N_LOG_PROBS [bs,seq,256] fp32`、`TOP_N_INDICES [bs,seq,256] int32`、`LOGPROBS_FLAG [1] int32`。

**开关 setter**：逐卡把标志槽填 1 或 0：

[tilert/models/deepseek_v3_2/modules/end2end.py:L694-L703](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L694-L703) — `set_logprobs_enabled` 用 `.fill_()` 置标志。

```python
def set_logprobs_enabled(self, enabled: bool) -> None:
    flag_val = 1 if enabled else 0
    for device_id in range(self.num_devices):
        intermediates, _, _, _ = self._get_device_result(device_id)
        intermediates[Idx.LOGPROBS_FLAG].fill_(flag_val)
```

注意它只 `.fill_()` 一个张量，没有 `go_home`、没有 `prepare_money`——这就是「不重捕图」的实证。

**只读 getter**：

[tilert/models/deepseek_v3_2/modules/end2end.py:L665-L680](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L665-L680) — `get_top_n_logprobs` 回读 `(TOP_N_LOG_PROBS, TOP_N_INDICES)`。

[tilert/models/deepseek_v3_2/modules/end2end.py:L682-L692](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L682-L692) — `get_token_logprob` 回读 `TOP_P_SCORES`（采样到的那一个 token 的 log-prob，单值）。

这些 getter 只是「读 temp_vars 的某个槽」，本身零开销，可在任意解码步后调用。

**CLI 入口**：

[tilert/generate.py:L146-L148](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/generate.py#L146-L148) — `--enable-logprobs` 是 `store_true` 开关，help 文本写明 "top-256 logprobs export (for benchmarking overhead)"。

[tilert/generate.py:L220-L225](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/generate.py#L220-L225) — 加载权重后、生成前调用 `set_logprobs_enabled(True)`。

```python
if args.enable_logprobs:
    if hasattr(generator.decode_layer, "set_logprobs_enabled"):
        generator.decode_layer.set_logprobs_enabled(True)
        print("Logprobs export enabled (top-256)")
```

这里有个健壮性细节：用 `hasattr` 探测方法是否存在，对不支持 logprobs 的生成器类型只打印告警而不崩溃。

#### 4.3.3 三类采样相关槽的「改动代价」对比

把本讲三个模块串起来，可以用一张表收尾：

| 改动对象 | 触发 API | 是否重捕 CUDA Graph | 原因 |
|----------|----------|--------------------|------|
| 采样配置四元组 | `update_sampling_config` | **是**（除非判等短路） | 可能改变录制的 kernel / launch 几何 |
| 请求级种子 | `set_sampling_seed` | 否 | 只写种子状态，图照常回放 |
| logprobs 开关 | `set_logprobs_enabled` | 否 | 只置一个标志张量，kernel 多写两块输出 |

这张表是本讲最重要的结论：**判断一个改动是否要重捕图，看它是否改变「图里录了什么 kernel」，而非「图运行时读到的数值」。**

#### 4.3.4 代码实践

**实践目标**：在不重捕图的前提下开启 logprobs 导出，并回读 top-256。

**操作步骤**（编程型，需 8 卡环境，否则标注「待本地验证」）：

1. 按 u1-l5 的方式构造 `DSAv32Generator` 并 `from_pretrained` 加载权重。
2. 调 `generator.decode_layer.set_logprobs_enabled(True)`。
3. 运行 `generator.generate("你好")`。
4. 生成后调 `generator.decode_layer.get_top_n_logprobs(0)`，打印返回的 `(log_probs, token_ids)` 的 shape。
5. 对照实验：在第 2 步前后分别记录首 token 延迟，观察是否因开启 logprobs 而明显变化。

**预期结果**：第 4 步得到 shape 为 `[1, seq, 256]` 的两个张量；第 5 步应当看到首 token 延迟**几乎不变**（因为没重捕图，仅多了写 top-256 输出的开销），这与 `update_sampling_config` 触发的「重捕图导致首 token 延迟陡增」形成鲜明对比。

**待本地验证**：若手边无 8 卡环境，改为源码阅读型——在 `generate.py` 第 220–225 行确认 `--enable-logprobs` 的调用点位于「权重加载之后、生成之前」，并解释为何放在这个位置（图已捕获，置标志即可影响后续回放）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `LOGPROBS_FLAG` 的 shape 是 `[1]` 而不是 `[bs, seq]`？
**参考答案**：它是「全局开关」——整个 batch、整条序列要么导出要么不导出，一个标量足够；kernel 每步回放时读这一个值决定是否写 top-256 输出。

**练习 2**：如果想在「请求 A 不导出、请求 B 导出」之间切换，是否需要重捕图？
**参考答案**：不需要。两次请求之间调 `set_logprobs_enabled(False)` / `set_logprobs_enabled(True)` 即可，都只是 `.fill_()` 一个张量，图保持不变。

---

## 5. 综合实践

把三个模块串成一个端到端小任务：**用程序化 API 跑一次「三模式切换 + logprobs 采集」的可复现基准**。

要求：

1. 构造一个 `DSAv32Generator`（`with_mtp=True`），加载权重。
2. 依次用 `update_sampling_params` 切换三种配置：
   - 模式甲：`use_topp=False`（top-1）
   - 模式乙：`use_topp=True, top_p=0.9`
   - 模式丙：`use_topp=True, top_p=0.95`
3. 每种配置下，先用固定 `sampling_seed=42` 跑两次相同 prompt，验证产出**可复现**（两次一致）；再换 `sampling_seed=7` 跑一次，验证产出**随种子变化**。
4. 在模式乙的第二次跑之前调 `set_logprobs_enabled(True)`，跑完用 `get_top_n_logprobs(0)` 取回 top-256，确认 shape 为 `[1, seq, 256]`。
5. 记录每种模式首次 `generate` 的首 token 延迟，观察模式切换处（甲→乙、乙→丙）是否出现「重捕图」导致的延迟尖峰；同一模式内连续两次跑是否因判等短路而无尖峰。

**验收点**：

- 模式切换处日志出现 `Recapturing CUDA graphs: ...`，且首 token 延迟明显升高；
- 同模式内第二次跑无该日志、无延迟尖峰（判等短路生效）；
- 固定种子两次产出相同、换种子产出不同（`set_sampling_seed` 语义正确）；
- logprobs 开关不触发重捕日志（开关式导出生效）。

若无法在 GPU 上运行，请至少完成源码阅读部分：在 `end2end.py` 与 `generator.py` 里标注出上述每一步对应的函数与行号，并写出「重捕 / 不重捕」的判断依据。

## 6. 本讲小结

- 采样配置被打包进一个 4 元 FP32 张量 `SAMPLING_CONFIG`（`Idx=48`），存 `(temperature, top_p, top_k, use_topp)`；请求级种子存 `SAMPLING_SEED`（`Idx=46`），二者改动代价不同。
- 由于连续存储会清零，`_init_weights` 必须在 `generate_params_with_continuous_storage` 之后用 `.copy_()` 把 `SAMPLING_CONFIG` 写回——这是承接 u2-l5 的隐蔽但关键的一步。
- `update_sampling_config` 用四元组判等做短路：没变直接 `return`；变了则 `go_home` 释放旧图 → 改写配置槽 → `prepare_money` 重捕新图，MTP 模式下释放与重捕各两次。
- 判等短路的价值在于：基准套件 `apply_mode` 在模式间循环时，同配置相邻两次免去重捕开销。
- `set_sampling_seed` 走独立 C++ 算子，按请求设一次、不重捕图；种子固定、位置推进，兼顾「可复现」与「每步随机」。
- logprobs 导出是「开关式」：`LOGPROBS_FLAG` 是普通张量，`set_logprobs_enabled` 仅 `.fill_()`，top-256 结果从 `TOP_N_LOG_PROBS / TOP_N_INDICES` 只读回读，全程不触发图重捕获。

## 7. 下一步学习建议

- 想看「重捕图」之外的另一种避免重捕思路，可继续读 u3-l3（MTP 投机解码），观察 `with_mtp` 如何通过「主模型子图 + 完整图」两张图的组合，在不同阶段复用而非重捕。
- 想理解采样参数如何参与基准的多模式对比，进入 u3-l5（基准测试套件），看 `BenchMode` 与 `apply_mode` 如何把本讲的 `update_sampling_params` 串成一张汇总表。
- 若对「图捕获后还能改哪些东西」感兴趣，建议回头精读 u2-l3（`ShowHandsDSALayer`）的 `prepare_money` 段落与 u2-l5（三层张量契约），它们解释了「哪些张量是图固化时绑定的、哪些是回放时可写的」，这是判断一切「要不要重捕图」问题的根本依据。
