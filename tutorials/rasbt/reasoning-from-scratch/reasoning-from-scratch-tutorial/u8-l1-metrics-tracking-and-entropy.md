# u8-l1 训练跟踪、可视化与熵

## 1. 本讲目标

第 6 章结束时，我们已经能跑通 GRPO 训练循环（u7-l4），但训练日志里只有 `loss`、`reward_avg`、`avg_response_len` 三个指标——它们回答不了这些问题：模型还有没有学习信号？输出分布是越来越自信还是越来越随机？训练后期准确率为什么回落？

本讲是单元八（GRPO 改进）的第一讲，主题是「先学会看，再动手改」。学完后你应当能够：

1. 用 `ch07.py` 的 `moving_average` 与 `plot_grpo_metrics` 把训练 CSV 日志画成诊断图，并解释每个设计细节（稀疏列、柱状图、尾部窗口）。
2. 写出熵的数学定义 \( H(p) = -\sum_v p_v \log p_v \)，并逐行读懂 `sequence_logprob_and_entropy` 如何在一次前向里「免费」算出答案区每个 token 的平均熵。
3. 用 `compute_advantage_stats` 解释为什么优势均值恒为 0、优势标准差才是学习信号的「体温计」，并能从 CSV 里的 `adv_std` 反推奖励分布。
4. 读懂 `7_3_plus_tracking.py` 相对第 6 章脚本新增的日志链路，并基于作者发布的 500 步真实日志诊断出「奖励上升、熵爆炸、评测分回落」的训练失稳现象——这正是后续三讲（clip ratio、KL、格式奖励）要解决的问题。

本讲全程**不需要 GPU**：所有实践都基于库函数、假模型和仓库自带的真实日志文件。

## 2. 前置知识

本讲用到四个前置概念，前三个在前面讲义已建立，这里只做一句话回顾与补全：

- **优势（advantage，u7-l3）**：GRPO 把同一 prompt 的多个 rollout 的奖励做组内标准化，\( A_i = (r_i - \mu) / (\sigma + 10^{-4}) \)，其中 \(\mu\)、\(\sigma\) 是组内奖励的均值与**样本**标准差（`torch.std` 默认除以 \( N-1 \)）。全对或全错时优势全零，模型不更新。
- **teacher forcing 打分（u6-l1）**：把「prompt + 已生成答案」整段送入模型一次前向，因果掩码保证每个位置都能合法地给出下一个 token 的分布；再用 `gather` 错位抽出「真实发生的那个 token」的 logprob。
- **追加式 CSV 日志（u7-l4）**：训练脚本每步向 CSV 追加一行，表头只在文件不存在时写入一次。本讲会看到这个设计的一个真实副作用。
- **熵（entropy，本讲新增）**：信息论里衡量一个概率分布「有多不确定」的量。直觉：抛均匀硬币（p=0.5/0.5）比偏向一面的硬币（p=0.99/0.01）更难猜，熵也更大。对语言模型来说，熵就是「模型对下一个 token 有多犹豫」。本讲用自然对数，单位是 **nat**。

一个后面反复用到的数字：Qwen3 的词表大小是 151936（u2-l1），所以模型每步输出分布的熵上限是

\[ \log 151936 \approx 11.93 \ \text{nats} \]

（均匀分布时熵最大，等于 \(\log V\)；one-hot 分布时熵为 0。）记住 11.93 这个天花板，第 4.4 节看真实日志时它会再次出现。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [reasoning_from_scratch/ch07.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch07.py) | 第 7 章工具函数库：`moving_average`、`plot_grpo_metrics`、`compute_advantage_stats`、`sequence_logprob_and_entropy`（以及下一讲之后会用到的 `reward_format`） |
| [ch07/03_rlvr_grpo_scripts_advanced/7_3_plus_tracking.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_3_plus_tracking.py) | 在第 6 章训练脚本基础上加入指标追踪的完整可运行脚本（约 520 行，自包含） |
| [ch07/02_logs/7_3_plus_tracking_metrics.csv](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/02_logs/7_3_plus_tracking_metrics.csv) | 作者发布的 7.3 节实验真实日志：500 步 × 10 列 |
| [ch07/02_logs/ch06_rlvr_grpo_original_no_kl_metrics.csv](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/02_logs/ch06_rlvr_grpo_original_no_kl_metrics.csv) | 第 6 章基础 GRPO 的 500 步日志，只有 7 列，用于对照「新增了哪些仪表」 |
| [tests/test_ch07.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch07.py) | 上述函数的单元测试：用假模型、假 CSV 验证行为 |
| ch07/01_main-chapter-code/ch07_main.ipynb | 章节主 notebook 的 7.2/7.3 节，是本讲的教学来源，建议对照阅读 |

两份 CSV 的表头对照（这是本讲的主线之一）：

```text
ch06: step,total_steps,loss,reward_avg,tokens_per_sec,avg_response_len,eval_acc
7_3:  step,total_steps,loss,reward_avg,tokens_per_sec,avg_response_len,adv_avg,adv_std,entropy_avg,eval_acc
```

7_3 版新增的三列 `adv_avg`、`adv_std`、`entropy_avg` 正是本讲 4.2、4.3 两节的产物。

## 4. 核心概念与源码讲解

本讲的三个最小模块——`plot_grpo_metrics` / `moving_average`、`sequence_logprob_and_entropy`、`compute_advantage_stats`——分别对应「图（怎么看）」「熵（看什么）」「优势统计（看什么）」，最后一个模块把它们接进训练循环。

### 4.1 模块一：`moving_average` 与 `plot_grpo_metrics`——把 CSV 日志变成诊断图

#### 4.1.1 概念说明

GRPO 每步只对**一条** prompt 采样少量 rollout（脚本默认 8 个），所以单步指标噪声极大：`reward_avg` 只有 9 个离散取值（0, 0.125, …, 1.0），`loss` 更是在正负几百之间跳。直接画原始曲线，只能看到毛刺，看不到趋势。

解决办法是两层：

1. **滑动平均**平滑单条曲线：每个点用它前面一小段窗口的均值替代，突出长期趋势。
2. **四宫格对比**：把 loss、reward、响应长度、评测准确率画在共享 x 轴（step）的 2×2 网格里——因为任何单一指标都可能骗人，只有并排看才有效果（第 4.4 节会给出真实例子）。

#### 4.1.2 核心流程

`plot_grpo_metrics(csv_path, columns)` 的执行过程：

```text
1. csv.DictReader 逐行读日志，以 step 列为共享 x 轴
2. 对 columns 里每一列，独立收集 (step, value) 数值对；空字符串字段直接跳过
3. 创建 2×2 子图网格
4. 每列画两条线：原始值（alpha=0.4 半透明）+ 移动平均值（实线）
   ── 例外：eval_acc 列画柱状图（因为它每 50 步才有一个点）
5. 某列在 CSV 里完全不存在/全空 → 删除对应子图
6. 可选 save_as 保存图片，plt.show() 显示
```

`moving_average(values, window_fraction)` 是「尾部窗口」移动平均：窗口大小取 `int(window_fraction × len(values))`（下限 1），每个位置的输出是「从窗口起点到当前位置」的均值——序列开头不足一个完整窗口时，就用现有数据算，不补零、不截短。

#### 4.1.3 源码精读

先看平滑函数：

[moving_average 的窗口与循环](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch07.py#L42-L52)——窗口大小随序列长度自适应（第 44 行 `max(1, int(window_fraction * len(values)))`），第 48–50 行对每个下标取 `values[start_idx : i+1]` 的均值，其中 `start_idx = max(0, i - window_size + 1)` 保证开头不越界。

它的精确行为有单元测试锁定：[tests/test_ch07.py:L61-L68](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch07.py#L61-L68) 断言 `moving_average([1, 3, 5, 7], window_fraction=0.5)` 等于 `[1, 2, 4, 6]`（窗口大小 2：相邻两点的均值），且 `window_fraction=0.0` 时退化为原序列（窗口下限 1）。手算验证：i=0 只有自己 → 1；(1+3)/2=2；(3+5)/2=4；(5+7)/2=6。✓

再看绘图函数的三个关键点：

[CSV 解析与稀疏列跳过](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch07.py#L59-L72)——注意两件事：一是 `Path(csv_path).open(newline="")`，把换行符处理交给 `csv` 模块（仓库里的真实日志是 Windows 风格 CRLF 行尾，这样打开才能正确解析）；二是第 69–70 行的 `if value_str:`，空字符串直接跳过。**这就是稀疏的 `eval_acc` 列能与稠密列共存的原因**：评测只在每 50 步发生，其余行该字段为空，解析时自动缺省，且每列的 `(step, value)` 是独立收集的，缺一个点不影响其他列。

[2×2 网格与三种画法](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch07.py#L74-L98)——第 84–85 行：该列没有任何数据时 `fig.delaxes` 删掉空格子（所以传 3 列也能用，第 4 格自动消失）；第 88–89 行：`eval_acc` 单独走柱状图分支（`width=20` 让稀疏的柱子在 step 轴上明显可见）；第 91–92 行：其余列画「原始值 `alpha=0.4` + 移动平均」两条线，半透明的是原始毛刺，实线是趋势。

#### 4.1.4 代码实践

**实践目标**：把作者发布的 7.3 节真实日志画成图，亲眼看到本讲后面要分析的所有现象。

**操作步骤**（示例代码，在仓库根目录运行）：

```python
# 示例代码：plot_7_3_metrics.py
import matplotlib
matplotlib.use("Agg")  # 无显示器环境下强制使用离屏后端（与 tests/test_ch07.py 的做法一致）
from reasoning_from_scratch.ch07 import plot_grpo_metrics

plot_grpo_metrics(
    "ch07/02_logs/7_3_plus_tracking_metrics.csv",
    columns=["loss", "reward_avg", "entropy_avg", "eval_acc"],
    save_as="7_3_metrics.png",
)
```

运行 `python plot_7_3_metrics.py`（需要已安装 `reasoning_from_scratch` 包与 `matplotlib`，见 u1-l2）。

**需要观察的现象**：

1. `loss` 子图：前半段在 0 附近抖动，后期出现数百量级的正负尖峰（第 500 步是 -916.8）。
2. `reward_avg` 子图：从 0 单调爬升到后期大片 1.0。
3. `entropy_avg` 子图：开头约 1.3，前 50 步内迅速跌到 0.1–0.4 区间，约 200 步之后开始抬升，300 步附近冲到 10 以上（逼近 11.93 的天花板）。
4. `eval_acc` 柱状图：15.6 → 46.2（step 50）见顶后一路回落到 33.0（step 500）。

**预期结果**：生成 `7_3_metrics.png`，一张 2×2 四宫格图。图中 step 1–5 附近的点会比想象中「多」一点——这是日志文件本身的特点，原因见 4.4.3 的最后一段。具体像素级效果待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `eval_acc` 用柱状图而不用和其他列一样的折线？
**答案**：`eval_acc` 每 50 步才有一个值（评测昂贵，见 4.4.3），是稀疏列。若画折线会把相隔 50 步的点直接连线，视觉上暗示「中间连续变化」，产生误导；柱状图明确表达「这是离散时间点上的测量」。

**练习 2**：`moving_average(values, window_fraction=1.0)` 会返回什么？
**答案**：窗口大小等于序列长度，于是每个输出都是「从头到当前位置的累积平均」——一条从第一步开始不断吸收新数据的全局平滑曲线，越往后越不敏感于新数据。

**练习 3**：若日志中某一行 `loss` 字段为空但 `reward_avg` 有值，两张子图会怎样？
**答案**：`loss` 子图少一个数据点，`reward_avg` 子图完整——因为每列的 `steps`/`values` 是独立收集的（空字段在第 70 行被跳过），x 轴记录的是 step 数值本身，缺行不会造成错位。

### 4.2 模块二：`compute_advantage_stats`——优势的体检指标

#### 4.2.1 概念说明

u7-l3 讲过，GRPO 的核心是组内标准化优势。第 7 章的新问题是：**这些优势本身能不能当训练健康的指标？** 能，而且取两个统计量：

- **`adv_avg`（优势均值）**：纯 sanity check。因为标准化时减掉了组内均值，优势均值在构造上**恒等于 0**（分子求和为 0，除以同一个常数仍是 0）。如果它漂移了，说明代码有 bug 或归一化出了问题。
- **`adv_std`（优势标准差）**：真正有信息量的指标。它回答「这一步还有多少学习信号」：
  - ≈ 1（实际上略小于 1，见下面练习 3）：奖励有分化，梯度信号量级健康；
  - → 0：rollout 全对或全错，优势全零，GRPO 在这一步学不到任何东西；
  - ≫ 1：奖励稀疏尖峰，更新过猛（本实现的奖励只有 0/1 两个值，所以实际不会出现这一档）。

注意一个容易混淆的点：**`reward_avg = 1.0` 与 `adv_std = 0` 是同一件事的两面**。奖励全 1 是好事（模型全答对），但意味着训练信号已耗尽——要么停止，要么换更难的题。所以看 `adv_std` 必须和 `reward_avg` 一起看。

#### 4.2.2 核心流程与数学

```text
输入: rewards_list（一组 rollout 的奖励，如 [1, 0, 1, 1]）
1. 与 GRPO 完全相同的标准化: A = (r - mean(r)) / (std(r) + 1e-4)
2. 新增统计: adv_avg = mean(A), adv_std = std(A)
返回: (advantages 张量, adv_avg 浮点, adv_std 浮点)
```

手算 `rewards = [1, 0, 1, 1]`（8 个 rollout 缩成 4 个便于手算）：

\[ \mu = 0.75, \qquad \sigma = \sqrt{\frac{3(0.25)^2 + (0.75)^2}{4-1}} = \sqrt{0.25} = 0.5 \]

\[ A = \frac{r - 0.75}{0.5 + 10^{-4}} \approx [0.4999,\ -1.4997,\ 0.4999,\ 0.4999] \]

均值精确为 0（因为 \( \sum_i (r_i - \mu) = 0 \)），这四个值的标准差约 0.9998——比 1 略小，原因是分母里加了 \( 10^{-4} \) 的防零 epsilon。

#### 4.2.3 源码精读

[compute_advantage_stats](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch07.py#L106-L115)——第 108–109 行复刻 GRPO 的优势公式（注释明确写着 "This is what we already compute in GRPO"），第 112–113 行只是对标准化结果再取 mean/std 并 `.item()` 转成 Python 浮点。

它对应的单元测试：[tests/test_ch07.py:L71-L80](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch07.py#L71-L80) 用 `rewards = [1.0, 2.0, 4.0]` 断言输出与手动标准化逐元素一致。

在训练脚本里，「统计量」被接进了损失函数的返回值和日志：

- [7_3_plus_tracking.py:L159-L160](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_3_plus_tracking.py#L159-L160)——`compute_grpo_loss` 内计算优势（与库版公式一字不差）。
- [7_3_plus_tracking.py:L185-L194](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_3_plus_tracking.py#L185-L194)——把标准化后的优势列表放进返回字典的 `"advantages"` 键。
- [7_3_plus_tracking.py:L314-L318](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_3_plus_tracking.py#L314-L318)——注意训练循环是对**已标准化的优势**再取 mean/std 写进日志（不是对原始奖励），所以 CSV 里的 `adv_avg` 列恒为 0.000000、`adv_std` 列要么 ≈0（全对/全错步）要么 ≈0.9997。

#### 4.2.4 代码实践

**实践目标**：亲手验证「优势均值恒为 0」以及几种退化情形，并从真实 CSV 的 `adv_std` 反推奖励分布。

**操作步骤**（示例代码）：

```python
# 示例代码：adv_stats_check.py
from reasoning_from_scratch.ch07 import compute_advantage_stats

# 任务指定的情形
adv, adv_avg, adv_std = compute_advantage_stats([1.0, 0.0, 1.0, 1.0])
print("advantages:", adv)          # 预期 ≈ [0.4999, -1.4997, 0.4999, 0.4999]
print("adv_avg =", adv_avg)        # 预期 ≈ 0（浮点残差在 1e-7 量级）
print("adv_std =", adv_std)        # 预期 ≈ 0.9998

# 两种无信号情形
for rewards in ([1.0, 1.0, 1.0, 1.0], [0.0, 0.0, 0.0, 0.0]):
    adv, avg, std = compute_advantage_stats(rewards)
    print(rewards, "-> mean", avg, "std", std)   # 预期均为 0.0 / 0.0
```

**需要观察的现象**：第一组输出的 `adv_avg` 打印出来是 0 或极接近 0 的数；后两组优势张量全是 0——这正是 7_3 脚本里 `skip_zero_adv`（跳过零优势更新，u7-l4 讲过）所要跳过的情形。

**预期结果**：与 4.2.2 的手算一致；确切的小数位待本地验证。

然后做一个「反推游戏」：打开 `ch07/02_logs/7_3_plus_tracking_metrics.csv`，你会发现 `adv_std` 列几乎只出现两种值：`0.000000` 和 `0.999717` / `0.999784` / `0.999807`。脚本 CLI 默认 8 个 rollout（`--num_rollouts` 默认值 8），对 0/1 奖励、k 个答对的组，样本标准差是

\[ \sigma_k = \sqrt{\frac{k(8-k)}{56}} \]

代入 `adv_std ≈ σ_k / (σ_k + 10^{-4})`：k=1 时 σ=0.3536 → 0.999717；k=2 时 σ=0.4629 → 0.999784；k=3 时 σ=0.5175 → 0.999807。与 CSV 中的取值一一对应（如第 2 行 `2,500,...,0.999717` 表示该步 8 个 rollout 恰好 1 个答对）。

#### 4.2.5 小练习与答案

**练习 1**：`compute_advantage_stats([1, 1, 1, 1])` 返回什么？它在 GRPO 里意味着什么？
**答案**：优势张量全 0，mean=0、std=0。奖励无分化 → 优势全零 → 策略梯度损失为 0，该步不产生任何参数更新（7_3 脚本可用 `--skip-zero-advantage-updates` 直接跳过这次昂贵的 backward）。

**练习 2**：CSV 中某步 `adv_std = 0.999717`、`reward_avg = 0.125`，这两个数一致吗？
**答案**：一致。0.125 = 1/8 说明 8 个 rollout 恰好 1 个答对；由练习公式 k=1 得 σ=0.3536，标准化后 std≈0.999717。两个指标互为印证。

**练习 3**：为什么 `adv_avg` 恒为 0，而 `adv_std` 不恰好等于 1？
**答案**：均值恒为 0 是代数结果——每个优势的分子都减去了 μ，求和必为 0。而标准化的分母是 \( \sigma + 10^{-4} \) 而非 σ，所以标准化后的方差是 \( \sigma^2/(\sigma+10^{-4})^2 < 1 \)，std 落在 0.9997–0.9999 一带而非精确 1。

### 4.3 模块三：`sequence_logprob_and_entropy`——把打分器升级成温度计

#### 4.3.1 概念说明

u6-l1 的打分器回答「模型给它**实际生成**的 token 打了多少分」（用 `gather` 只抽取真实 token 的 logprob）。但它扔掉了分布里的其余 151935 个 token——而模型的「犹豫程度」恰恰藏在整个分布里。

熵补上这一维：对每个答案位置，把**全词表**的 \( p \cdot \log p \) 加权求和：

\[ H_t = -\sum_{v=1}^{V} p_v^{(t)} \log p_v^{(t)} \]

直觉读法：如果模型把概率集中在一个 token 上（one-hot），\( H = 0 \)，模型「毫不犹豫」；如果概率摊平到全词表（均匀分布），\( H = \log V \approx 11.93 \)，模型「完全随缘」。

工程上最妙的一点是：**熵几乎是免费的**。teacher-forcing 前向 + `log_softmax` 已经算出了全词表的 logprob 矩阵，旧代码只用 `gather` 从中抽走一列；熵只是对同一矩阵多做一次「以概率为权重的求和」。代价只是把「每步抽一个数」变成「每步对 15 万个数求和」，无需额外前向。

判断标尺（来自主 notebook 7.3.2 节的经验法则）：

| 平均熵（nats） | 分布状态 | 训练含义 |
| --- | --- | --- |
| ≈ 0–0.5 | 一个 token 压倒性占优 | 高度自信，近乎确定性行为 |
| ≈ 1–2 | 概率分给少数几个 token | 训练稳定期的典型状态 |
| ≫ 2，趋向 11.93 | 概率摊到大量 token | 接近随机，多半出了问题 |

在 GRPO 语境里：熵太低 → 探索枯竭、可能坍缩成复读机；熵冲向天花板 → 策略已发散成噪声。两种都要避免。

#### 4.3.2 核心流程与数学

函数对一个 rollout（`token_ids` 含 prompt + 答案，`prompt_len` 是 prompt 长度）执行：

```text
1. 一次 teacher-forcing 前向 → logits (T, V)，升 float32
2. log_softmax → logprobs (T, V)
3. 旧逻辑（与第 5 章相同）:
   gather 错位抽取真实 next-token 的 logprob → selected (T-1,)
   取答案区 selected[prompt_len-1:] 求和 → logp_all_steps     # 序列 logprob，梯度入口
4. 新逻辑（熵）:
   取答案区全词表分布 all_answer_logprobs = logprobs[:-1][prompt_len-1:]
   exp 还原概率 → p
   逐元素 p * logp，沿词表维 (dim=-1) 求和取负 → step_entropy   # 每个答案 token 一个熵
   对答案步求平均 → entropy_all_steps
5. 边界保护: 答案区为空（模型立刻输出 EOS）→ 熵记 0.0
返回 (logp_all_steps, entropy_all_steps)  # 两个都是带梯度的张量
```

两个设计选择值得停下来想：

- **logprob 求和、熵求平均**。序列 logprob 是似然，必须求和（乘积取对数）；熵是「强度」指标，若求和则长 rollout 的熵天然更大，与 u6-l1 讨论过的长度偏差同病，故取平均使长短 rollout 可比。
- **切片 `logprobs[:-1][prompt_len - 1:]`**。`[:-1]` 丢掉最后一步（它预测的是不存在的第 T+1 个 token）；`[prompt_len - 1:]` 从「第一个答案 token 的预测位」开始——与 u6-l1 `avg_logprob_answer` 的起点约定完全一致。

#### 4.3.3 源码精读

库版本：[sequence_logprob_and_entropy](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch07.py#L118-L140)。函数自带分界注释：第 120 行 `# Old: Code is identical to chapter 5` 以下是旧打分逻辑；第 130 行 `# New: Calculate entropy` 开始是新增部分。

熵的四行核心：[ch07.py:L131-L138](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch07.py#L131-L138)——第 132–133 行先做边界保护：`numel() == 0`（模型对 prompt 直接回 EOS，答案区为空）时返回一个 0 标量，且用 `logp_all_steps.new_tensor` 保证和 logprob 同设备同 dtype；第 135 行 `exp` 把 logprob 还原成概率（避免再算一次 softmax）；第 136 行逐元素 \( p \log p \)；第 137 行沿词表维求和取负得到每步熵；第 138 行对答案步取平均。

脚本内副本（注释更细）：[7_3_plus_tracking.py:L81-L102](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_3_plus_tracking.py#L81-L102)，逻辑与库版完全相同。

测试佐证：[tests/test_ch07.py:L83-L117](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch07.py#L83-L117)。前一个测试用 `DummyConstLogitModel`（[L25-L31](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch07.py#L25-L31)，无论输入什么都返回同一组 logits 的假模型）手动复算 logprob 与熵并断言一致；后一个测试构造 `prompt_len=3` 但序列只有 2 个 token 的「空答案」样例，断言两个输出都是 0.0。

最后澄清一个梯度问题：返回的熵张量**带着梯度**（它由 logits 算出，且函数内没有 `no_grad`），但在 7_3 脚本里它只被 `entropy.item()` 转成 Python 数写日志（[7_3_plus_tracking.py:L142-L147](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_3_plus_tracking.py#L142-L147)）——**本讲中熵是观察指标，不是损失项**。

#### 4.3.4 代码实践

**实践目标**：用一个「输出永远不变」的假模型，手算并验证熵实现。

**操作步骤**（示例代码，思路完全仿照 `tests/test_ch07.py` 的 `DummyConstLogitModel`）：

```python
# 示例代码：entropy_check.py
import torch
from reasoning_from_scratch.ch07 import sequence_logprob_and_entropy

class ConstLogitModel:
    """不管输入什么，每个位置都输出同一组 logits 的假模型（示例代码）"""
    def __init__(self, logits):
        self.logits = torch.tensor(logits, dtype=torch.float32)
    def __call__(self, token_ids):
        b, t = token_ids.shape
        return self.logits.repeat(b, t, 1)

model = ConstLogitModel([0.1, 0.3, 0.6])   # 3 个 token 的迷你词表
token_ids = torch.tensor([0, 2, 1, 2])     # 2 个 prompt token + 2 个答案 token
logp, entropy = sequence_logprob_and_entropy(model, token_ids, prompt_len=2)
print("logp =", logp.item(), " entropy =", entropy.item())
```

**手算预期**：`log_softmax([0.1, 0.3, 0.6])` 给出概率约 `[0.2583, 0.3155, 0.4262]`、logprob 约 `[-1.3533, -1.1533, -0.8533]`。

- 答案 token 是 id 1 和 id 2，所以 `logp ≈ -1.1533 + (-0.8533) = -2.0066`；
- 因为假模型输出恒定，每步熵相同：\( H = -(0.2583 \times -1.3533 + 0.3155 \times -1.1533 + 0.4262 \times -0.8533) \approx 1.0771 \) nats，平均后仍是 1.0771。

**需要观察的现象**：打印值与手算一致（浮点误差在 1e-4 量级内）。作为对照，3 词表均匀分布的熵上限是 \( \log 3 \approx 1.0986 \)——1.0771 已经非常接近上限，说明这组相当平的 logits 对应一个「很犹豫」的分布。

**预期结果**：`logp ≈ -2.007`，`entropy ≈ 1.077`；确切小数位待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：如果把第 138 行的 `torch.mean(step_entropy)` 改成 `torch.sum(step_entropy)`，日志会出什么问题？
**答案**：熵会随答案长度近似线性增长——一条 500 token 的 rollout 的「总熵」天然是一条 50 token rollout 的 10 倍，`entropy_avg` 就不再度量「模型每步有多犹豫」，而主要度量「答案有多长」。这正是 u6-l1 里用平均消除长度偏差的同一道理。

**练习 2**：一个对每个下一 token 都输出 one-hot 分布的模型，`entropy_all_steps` 是多少？输出均匀分布呢？
**答案**：one-hot 时 \( p \log p \) 只剩一项 \( 1 \cdot \log 1 = 0 \)，熵为 0；均匀分布时熵为 \( \log V \)，对 Qwen3 词表 \( \log 151936 \approx 11.93 \) nats。

**练习 3**：为什么需要「答案区为空 → 熵记 0」的保护？去掉它会怎样？
**答案**：模型可能对 prompt 立刻输出 EOS，答案区切片长度为 0，对空张量做 `mean` 会得到 NaN。NaN 一旦进入 `entropy_avg` 就会污染整步日志，甚至通过后续统计掩盖真实问题。测试 `test_sequence_logprob_and_entropy_handles_empty_answer_segment` 专门锁定这一行为。

### 4.4 模块四：`7_3_plus_tracking.py`——把三个仪表接进训练循环

#### 4.4.1 概念说明

前三个模块是零件，`7_3_plus_tracking.py` 是整机：它把 ch06 的训练脚本（u7-l4 的八阶段循环）复制过来，装上「仪表盘」。相对 ch06 版（其 CSV 表头见 [rlvr_grpo_original_no_kl.py:L221](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch06/02_rlvr_grpo_scripts_intro/rlvr_grpo_original_no_kl.py#L221)）的改动可以一句话概括：**训练循环的结构一步没变，变的是每一步「多记录了什么」**：

1. `compute_grpo_loss` 的返回字典新增 `entropies`（每个 rollout 的平均熵）、`advantages`（标准化后优势）、`is_zero_adv`、`samples`（样本文本 + 奖励 + 长度）；
2. 训练循环据此汇总 `entropy_avg`、`adv_avg`、`adv_std`，写进文本日志与 CSV（新增 3 列）；
3. 每 10 步把前 3 条 rollout 样本落盘到 `_outputs.txt`（训练黑匣子）；
4. 每逢 checkpoint 步可自动跑 MATH-500 子集评测，把 `eval_acc` 写进同一 CSV。

#### 4.4.2 核心流程

每一步的数据流（只画与「追踪」相关的部分）：

```text
for step in range(steps):
    stats = compute_grpo_loss(...)           # 8 次 rollout
        每次 rollout:
            sample_response            → token_ids, prompt_len, text
            sequence_logprob_and_entropy → (logp, entropy)   # 本讲模块三
            roll_entropies.append(entropy.item())
            reward_rlvr(text, answer)  → reward
        rewards → advantages (组内标准化)      # 本讲模块二
        is_zero_adv = 全零判断；可跳过该步更新
        pg_loss = -(advantages.detach() * logps).mean()
    (若 loss_tensor 非 None) backward → clip_grad_norm → step
    reward_avg / entropy_avg / adv_avg / adv_std / tokens_per_sec / avg_response_len
    每 10 步  → append_sample_logs（前 3 条样本）
    每 50 步  → save_checkpoint + evaluate_math500_stream → eval_acc
    append_step_metrics：一行文本日志 + 一行 10 列 CSV
```

#### 4.4.3 源码精读

**改动一：损失函数收集熵与样本**——[7_3_plus_tracking.py:L126-L154](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_3_plus_tracking.py#L126-L154)。第 126 行的四个收集列表里 `roll_entropies` 与 `samples` 是新增的；第 142 行一次调用同时拿到 logprob 与熵；第 147 行 `entropy.item()` 立即脱离计算图——熵只进日志、不进损失。

**改动二：零优势判断与跳过**——[7_3_plus_tracking.py:L159-L178](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_3_plus_tracking.py#L159-L178)。优势计算后用 `torch.allclose` 判断是否全零；`skip_zero_adv=True` 且全零时返回 `loss_tensor=None`，训练循环（[L307-L312](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_3_plus_tracking.py#L307-L312)）据此跳过整次 backward/step——钱花在 8 次 rollout 上了，但至少不再花在无效反传上。这就是 4.2 节「`adv_std=0` = 无信号」的工程出口。

**改动三：三份日志**——

- [append_sample_logs](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_3_plus_tracking.py#L197-L207)：每 10 步把前 3 条样本的「奖励 + 长度 + 换行转义后的文本」追加到 `_outputs.txt`——训练出问题时，先看这份「现场记录」。
- [append_step_metrics](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_3_plus_tracking.py#L210-L245)：一行人读的文本日志（L222–232）+ 一行机读的 CSV（L239–245）。关键细节在第 234–238 行：**表头只在文件不存在时写入**。这继承了 u7-l4 的追加式设计，也带来一个真实副作用，见本节最后一段。
- [append_eval_metrics](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_3_plus_tracking.py#L248-L254)：评测结果单独记一行文本。

**改动四：checkpoint 时的自动评测**——[7_3_plus_tracking.py:L329-L364](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_3_plus_tracking.py#L329-L364)。每逢 `checkpoint_every`（默认 50）整数倍步，先存 checkpoint，再（若 `eval_max_items > 0`）切 `eval()`、用第 3 章的 `evaluate_math500_stream` 在 MATH-500 子集上评测、写 jsonl 与 `eval_acc`、再恢复 `train()`。评测昂贵（生成 500 题答案），所以默认关闭（CLI `--eval_on_checkpoint` 默认 0），CSV 中绝大多数行的 `eval_acc` 是空字符串（[L240](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_3_plus_tracking.py#L240) 写空串）——这正是 4.1 节稀疏列处理的真实来源。

**一个真实数据的「坑」**：打开 `7_3_plus_tracking_metrics.csv` 看 `total_steps` 列——前 5 行是 `300`，之后 500 行是 `500`。这不是 bug：作者先启动了一次 300 步的运行（留下 5 行日志）后中断，重跑 500 步实验时旧 CSV 未删，追加式写入把两次运行拼在了同一文件里（表头只在文件不存在时写，所以列名只有一份）。于是 step 1–5 各出现两次，`plot_grpo_metrics` 以 step 为 x 轴会把它们画成重叠的点。教训：**复现实验前清理旧 CSV，或给日志文件名加时间戳/运行 ID**。

#### 4.4.4 代码实践

**实践目标**：基于真实日志，完成本讲规格要求的三个任务：画曲线、验证优势均值、总结 reward 与熵的反向关系。

**操作步骤**：

1. 画图：执行 4.1.4 的脚本（或用 pandas + matplotlib 手工复刻：`df = pd.read_csv("ch07/02_logs/7_3_plus_tracking_metrics.csv")`，过滤 `df[df.total_steps == 500]` 后对 `loss` / `reward_avg` / `entropy_avg` 画 `rolling(50).mean()` 曲线、对 `eval_acc` 去掉 NaN 后画柱状图）。
2. 验证优势均值：执行 4.2.4 的脚本，确认 `[1, 0, 1, 1]` 的 `adv_avg ≈ 0`。
3. 解读下表（数字取自 CSV 真实行，step 1 取自 300 残留运行的基线评测）：

| step | reward_avg | adv_std | entropy_avg | avg_response_len | eval_acc |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 0.000 | 0.000 | 1.330 | 6.4 | 15.6 |
| 50 | 1.000 | 0.000 | 0.202 | 337.1 | 46.2 |
| 200 | 0.375 | 0.9998 | 0.832 | 934.4 | 44.2 |
| 300 | 0.000 | 0.000 | 10.660 | 1024.0 | 37.0 |
| 350 | 0.000 | 0.000 | 11.180 | 1024.0 | 38.0 |
| 500 | 0.125 | 0.9997 | 2.615 | 762.3 | 33.0 |

**需要观察的现象**：

- **前 50 步**：`reward_avg` 0 → 1.0，`eval_acc` 15.6 → 46.2（约三倍），响应长度 6 → 337 token——模型迅速学会「写出解题过程再给 \boxed{}」。
- **熵先跌**：`entropy_avg` 从 1.33 掉到 0.2 一带——随着奖励信号把概率质量集中到被强化的 token 上，模型每步越来越自信。
- **约 200 步后熵疯涨**：`entropy_avg` 在 step 300/350 冲到 10.66 / 11.18——距 11.93 的均匀分布天花板只差一步，同时 `avg_response_len` 顶到 1024（`max_new_tokens` 上限，即模型从不收尾）、`reward_avg` 归零。三件事互相印证：策略已发散成接近随机的 token 生成器。
- **评测回落**：`eval_acc` 从 46.2 的峰值一路掉到 33.0，后期 `loss` 出现 -916.8 这类尖峰。主 notebook 7.3.3 节的解读是「熵升高 + adv_std 未消失 = 探索」，但把 `eval_acc` 柱状图并排看，这次运行后期更像失稳而非健康探索——**这正是「多指标并排看」的价值，也是引出 7.4 clip ratio、7.5 KL 两讲的动机**。

**预期结果**：图中能同时看到「奖励上升段」与「熵爆炸段」在时间上错开（先升熵跌、后熵爆奖励崩）。用一句话总结二者的关系（实践任务第 3 问的参考答案）：

> **`reward_avg` 与 `entropy_avg` 大体呈反向关系：奖励升高对应模型自信（每步熵低），而训练后期奖励崩塌时每步熵飙升至逼近 \(\log V \approx 11.93\) 的均匀随机水平——高奖励靠的是集中分布，高熵意味着模型已在全词表上「随缘」。**

精确的分段统计数字（如两者的相关系数）待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 CSV 里 `adv_avg` 列从头到尾都是 0.000000？它还有什么用？
**答案**：训练循环对**已标准化**的优势取均值（[L316-L317](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_3_plus_tracking.py#L316-L317)），组内减均值使它构造上恒为 0。它是 sanity check：若某天它漂移了，说明优势计算或日志链路被改坏。

**练习 2**：日志文件开头 5 行 `total_steps=300`、随后又从 step 1 重新开始且 `total_steps=500`，成因是什么？你会如何修复？
**答案**：`append_step_metrics` 以追加模式打开 CSV，表头仅在文件不存在时写入（L234–L238）；作者一次 300 步的试跑留下 5 行后被中断，正式 500 步运行直接续写同一文件。修复：跑新实验前删旧 CSV，或把运行参数/时间戳编进日志文件名。

**练习 3**：`train_rlvr_grpo` 函数签名里 `num_rollouts` 默认是 9（[L273](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_3_plus_tracking.py#L273)），但 CLI `--num_rollouts` 默认是 8（[L427-L431](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch07/03_rlvr_grpo_scripts_advanced/7_3_plus_tracking.py#L427-L431)）。命令行运行时实际用哪个？这解释了 4.2.4 中哪个数字？
**答案**：命令行运行时 argparse 的默认值 8 生效（`args.num_rollouts` 显式传入函数）。这解释了 `adv_std` 的取值 0.999717/0.999784/0.999807 恰好对应 8 个 rollout 中 1/2/3 个答对的组合。

## 5. 综合实践

写一个约 40 行的诊断脚本 `diagnose_grpo.py`（示例代码），把本讲三个模块串起来：

1. **读入** `ch07/02_logs/7_3_plus_tracking_metrics.csv`（pandas 可直接读，CRLF 行尾不影响），用 `df[df["total_steps"] == 500]` 过滤掉 300 步残留运行的 5 行（4.4.3 的坑）。
2. **分段体检**：以 50 步为窗口，输出一张「窗口内 `reward_avg` 均值 | `entropy_avg` 均值 | `adv_std` 非零比例 | 该窗口评测点的 `eval_acc`」对照表。
3. **相关性**：对 50 步窗口聚合后的两列计算皮尔逊相关系数（`df.corr()`），验证 4.4.4 的一句话结论。
4. **画图**：1×3 子图——`reward_avg` 与 `entropy_avg`（各叠加 50 点滚动均值，复刻 `moving_average` 的效果或直接调用它）、`eval_acc` 柱状图（复刻 `plot_grpo_metrics` 的稀疏列处理）。
5. **结论**：脚本末尾打印你对「这次训练应该在第几步附近早停、依据是哪几个指标同时报警」的判断。

锚点数据（可用于自检）：step 50 的 `eval_acc` 为 46.2、step 300 的 `entropy_avg` 为 10.659618、`avg_response_len` 为 1024.0。相关系数的精确值与最佳早停步数待本地验证。

进阶（可选）：把同一脚本跑在 `ch06_rlvr_grpo_original_no_kl_metrics.csv` 上（它没有 `adv_avg/adv_std/entropy_avg` 三列，注意容错），对比两次运行 `eval_acc` 轨迹的异同——ch06 版没有熵仪表时，你能仅凭 loss 和 reward 发现后期失稳吗？

## 6. 本讲小结

- `plot_grpo_metrics` 是「CSV → 四宫格诊断图」的仪表盘：空字段跳过让稀疏的 `eval_acc` 列与稠密列共存，`eval_acc` 用柱状图避免稀疏点连线误导，`moving_average` 用随长度自适应的尾部窗口（下限 1）平滑毛刺。
- `compute_advantage_stats`：优势均值在构造上恒为 0，是 sanity check；优势标准差 ≈1（因 \(10^{-4}\) epsilon 而略小于 1）表示学习信号健康，=0 表示该步 rollout 全对或全错、GRPO 无梯度可用（工程出口是 `skip_zero_adv`）。
- `sequence_logprob_and_entropy` 在 teacher-forcing 的 `log_softmax` 输出上「免费」算熵：\( H_t = -\sum_v p_v \log p_v \) 对全词表求和得每步熵，再对答案步取平均消除长度偏差；上限 \(\log V \approx 11.93\) nats，空答案区有 NaN 保护。
- `7_3_plus_tracking.py` = ch06 训练循环 + 仪表盘：损失函数多返回熵/优势/样本，CSV 多三列，每 10 步落盘样本日志，每 50 步 checkpoint 可选自动 MATH-500 评测；追加式日志会把中断后重跑的多次运行拼进同一文件（真实日志前 5 行的 `total_steps=300` 残迹）。
- 真实 500 步日志的诊断：奖励 0→1、eval 15.6→46.2 的快速收益段对应熵从 1.33 跌到 0.2；约 200 步后熵反向飙升至 10+（逼近均匀分布）、响应顶满 1024 上限、奖励归零、eval 回落到 33.0——**单一指标都会骗人，reward 与熵大体反向，必须并排看**。

## 7. 下一步学习建议

本讲看到的现象——loss 尖峰、熵爆炸、评测回落——正是策略更新步子迈得太大的症状。下一讲 **u8-l2「GRPO 改进一：Clip Ratio 裁剪」**将给出第一个药方：在 rollout 时记录旧 logprob，训练时用被 `clamp` 裁剪的新旧策略比值限制单步更新幅度。阅读时建议：

1. 先读 `ch07/03_rlvr_grpo_scripts_advanced/7_4_plus_clip_ratio.py`，与 7_3 脚本 diff 着看，改动集中在 `compute_grpo_loss`；
2. 用本讲的 `plot_grpo_metrics` 画 `ch07/02_logs/7_4_plus_clip_ratio_metrics.csv`（多了 `policy_ratio` 列），对比 7_3 的熵曲线是否不再爆炸；
3. 回头看 `tests/test_ch07.py` 后半部分关于裁剪方向的 5 个测试，它们预演了 u8-l2 的核心考点；
4. 若想巩固本讲，可通读主 notebook 的 7.2–7.3 节，那里有熵计算的图解版本。
