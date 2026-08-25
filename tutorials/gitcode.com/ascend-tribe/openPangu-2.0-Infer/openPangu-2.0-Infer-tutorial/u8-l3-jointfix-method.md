# JointFix 方法:联合 (a,b) 平滑参数搜索

## 1. 本讲目标

本讲是 jointfix 单元的第三讲。u8-l1 讲清了工具的三层正交架构(backends × methods × core),u8-l2 讲了 core 层"逐层前向收统计 → 量化 → 重前向验证"的 runner 主循环。本讲进入 **methods/ 目录里的核心方法 JointFix**,回答三个问题:

1. **为什么需要平滑(smooth)**:W8A8 量化误差主要来自激活中的离群通道,平滑参数 \(s\) 如何在不改变数学结果的前提下把"量化难度"在激活侧与权重侧之间重新分配,以及联合 (a,b) 如何比经典 SmoothQuant 的固定配比搜到更优点。
2. **坐标下降如何交替优化**:gate/up 与 down 两组平滑参数相互耦合,K=2 迭代如何配合"统计量解析刷新"收敛。
3. **误差往哪边最小化**:输出重建(output-recon)目标为什么比权重误差更可信;写出阶段为什么输出侧用 GPTQ、输入侧用 RTN。

学完本讲,你应该能读懂 `jointfix quantize` 命令里 `--num-iterations 2 --iter-ab-tol 0.05 --objective output-recon --write-quant gptq --skip-shared-experts` 每一个开关背后的源码依据,并能解释 `joint_search_traces.json` 里每个字段的来源。

## 2. 前置知识

### 2.1 W8A8 与量化误差的来源

回顾 u8-l1:W8A8 = 权重 8bit per-output-channel 静态量化 + 激活 8bit per-token 动态量化。INT8 对称量化的基本操作是:

\[
q = \operatorname{round}(x / \text{scale}), \quad \hat{x} = q \cdot \text{scale}, \quad \text{scale} = \frac{\max|x|}{127}
\]

scale 由被量化张量在量化粒度上的绝对值最大者决定。**问题在于大模型激活里存在少数"离群通道"(outlier channel)**:某些 hidden 维度上的激活值比其他通道大几十倍。per-token 量化时,scale 被这些离群值撑大,其余通道全部被"压扁"到低精度区域,量化误差急剧上升。

### 2.2 SmoothQuant 的直觉

对一个线性层 \(Y = X W^\top\)(\(X\) 按最后一维 \(h_{in}\) 逐通道),取一个逐通道缩放向量 \(s \in \mathbb{R}^{h_{in}}\),做数学等价变换:

\[
Y = (X \oslash s)\,(W \odot s)^\top
\]

其中 \(\oslash\)、\(\odot\) 都是按输入通道(h_in 维)逐元素操作。**精确算术下结果不变**,但量化时:激活除以 \(s\)(离群通道的 \(s\) 大)把激活分布"压平",激活量化误差下降;权重乘以 \(s\) 后权重误差可能上升。\(s\) 的取法就是在两侧误差之间找平衡。经典 SmoothQuant 取 \(s = \sqrt{x_{\max} / w_{\max}}\),即固定的"对半分"。

JointFix 把它推广成**二维搜索**:\(s_c = x_c^a / w_c^b\),让数据自己决定配比,而不是拍脑袋对半分。

### 2.3 本讲要用到的几个算法名词

- **格点搜索(grid search)**:把候选值列成网格,逐个评估目标函数取最小。简单、可并行、无梯度。
- **坐标下降(coordinate descent)**:多个变量耦合时,轮流固定其他变量、优化一个变量,循环直到收敛。
- **RTN(round-to-nearest)**:最朴素的逐行取整量化。
- **GPTQ**:利用校准样本的二阶信息(Hessian)做误差补偿的量化算法——量化完一列后,把这一列的量化误差按 Hessian 逆的信息"摊派"到还没量化的列上,使整体输出误差最小。
- **fake quantize(伪量化)**:量化再反量化,得到"如果真量化会是什么值"的模拟结果,用于在搜索阶段评估误差而无需真正落盘 INT8。
- **闭包(closure)**:Python 里函数内部定义的函数,捕获外层变量。本讲搜索驱动器把目标函数封装成 `J(a, b)` / `J_batch(a_list, b_list)` 两个闭包传给通用搜索逻辑,这是单设备与分布式共享同一套搜索代码的关键。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [tools/quant/jointfix/jointfix/methods/base.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/methods/base.py) | `QuantMethod` 抽象基类——方法轴的接口契约,本讲所有代码都在实现这个接口 |
| [tools/quant/jointfix/jointfix/methods/jointfix.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/methods/jointfix.py) | JointFix 方法主体:超参配置类、两阶段 (a,b) 搜索驱动器、`JointFixMethod` 类 |
| [tools/quant/jointfix/jointfix/methods/smooth_search.py](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/methods/smooth_search.py) | 纯数学叶子模块:平滑因子构造、通道加权、输出重建目标(单个/批量/分布式) |
| [tools/quant/jointfix/jointfix/methods/_smooth_pangu.py](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/methods/_smooth_pangu.py) | Pangu 耦合层:一个 decoder 层的完整"平滑吸收拓扑 + K 轮坐标下降 + 混合量化写出" |
| [tools/quant/jointfix/jointfix/core/primitives.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/core/primitives.py) | 底层量化原语:`int8_fake_quantize`、`rtn_quantize`、`gptq_quantize`、写出分发 |
| [tools/quant/jointfix/jointfix/core/stats.py](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/core/stats.py) | 统计收集器与 `refresh_stats_after_smooth`(迭代间的解析刷新) |
| [tools/quant/jointfix/examples/smoke_pangu_layer.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/examples/smoke_pangu_layer.py) | 单层冒烟测试:验证后端建层 + 前向链路(注意:不含量化) |
| [tools/quant/jointfix/docs/quantize_openpangu_w8a8.md](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/docs/quantize_openpangu_w8a8.md) | 官方量化操作文档,生产命令与参数推荐值的出处 |

## 4. 核心概念与源码讲解

### 4.1 平滑量化与联合 (a,b) 平滑因子

#### 4.1.1 概念说明

本模块回答:平滑因子 \(s\) 的数学形式是什么,(a,b) 两个自由度各控制什么,以及搜索空间如何组织。

- \(s_c = x_c^a / w_c^b\),其中 \(x_c\) 是通道 \(c\) 上激活幅值的统计量(默认 amax),\(w_c\) 是该输入通道对应权重列的绝对值最大值。
- \(a\) 控制激活统计进入分子多少:激活离群越严重,最优 \(a\) 越大,激活被压得越平。
- \(b\) 控制权重统计进入分母多少:权重本身在通道 \(c\) 上幅值大时,\(s_c\) 变小,避免把权重侧误差放大。
- \(a=b=0\) 时 \(s \equiv 1\),即"不平滑";\(a=b=0.5\) 退化为经典 SmoothQuant。

注意 \(a\)、\(b\) 是**两个独立旋钮**而非强制 \(a+b=1\):搜索第一阶段沿反对角线(\(a+b=1\))粗扫,第二阶段的局部方框搜索可以走出反对角线。

#### 4.1.2 核心流程

两阶段搜索(单层内、每个"平滑组"各做一次):

```text
输入: 激活统计 x_stat(amax)、权重统计 w_stat、目标函数 J(a,b)
1. 计算 J(0,0) 作为基线(不平滑的误差)
2. Stage 1 粗搜: 在反对角线网格 {(0.1,0.9),(0.2,0.8),…,(0.9,0.1)} 共 9 点上
   批量评估 J,取最小者为 (a1,b1)
3. Stage 2 细搜: 以 (a1,b1) 为中心、半径 0.2、步长 0.1 的 5×5 二维方框
   (越界裁剪到 [0,1]) 共 25 点,再批量评估 J,与 Stage 1 最优比较取更优者
4. 安全网: 若 J(0,0) 比所有搜索出的候选都小 → 强制回到 s=1(搜索永不劣化)
5. 用最优 (a*,b*) 构造 s 并返回,附带 trace(a、b、J、各阶段耗时等诊断)
```

安全网保证一个重要不变量:**搜索结果永远不会比不平滑更差**——因为网格里不含 (0,0),万一校准样本本身没有离群问题,搜索也能优雅地退回无操作。

#### 4.1.3 源码精读

平滑因子的构造只有一行核心公式,位于数学叶子模块:

- [methods/smooth_search.py:L28-L34](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/methods/smooth_search.py#L28-L34) —— `make_smooth_scale` 实现 \(s_c = \text{clamp}(x_c)^{a} / \text{clamp}(w_c)^{b}\),clamp 下限 eps 防止 0 的负幂产生 inf;docstring 明确写出两个特例:a=b=0 → s=1,a=b=0.5 → SmoothQuant。

所有搜索超参集中在 `JointSearchConfig` 数据类里,**刻意与方法同住、不放进 core**(core 永远不 import 它,保持方法无关):

- [methods/jointfix.py:L45-L58](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/methods/jointfix.py#L45-L58) —— Stage 1 反对角网格(9 个默认候选点)、Stage 2 半径/步长,以及 `write_quant`/`gptq_damp`/`gptq_block_size`/`objective` 四个写出侧旋钮。
- [methods/jointfix.py:L73-L83](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/methods/jointfix.py#L73-L83) —— 坐标下降的 `num_iterations`(代码默认 1,生产推荐 2)与收敛阈值 `iter_ab_tol`(0.05),以及 `skip_shared_experts` 开关(见 4.4)。

搜索主体是一个**与设备无关的通用驱动器** `_two_stage_ab_search`,它不关心 J 怎么算——单设备与分布式只是传入不同的 `J`/`J_batch` 闭包:

- [methods/jointfix.py:L150-L166](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/methods/jointfix.py#L150-L166) —— 先算 `J_at_zero = J(0.0, 0.0)` 基线;L1 早跳过探针默认关闭(docstring 注明"单点探针会误判")。
- [methods/jointfix.py:L168-L181](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/methods/jointfix.py#L168-L181) —— Stage 1:把 9 个候选 (a,b) 打包成两个列表交给 `J_batch` 批量评估,`min` 选出最优下标。若有 `warm_start`(上一轮迭代的 (a,b))则直接跳过 Stage 1——这是为坐标下降预留的种子接口。
- [methods/jointfix.py:L195-L216](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/methods/jointfix.py#L195-L216) —— Stage 2:围绕 (a1,b1) 构造 5×5 方框(越界裁剪到 [0,1]),批量评估;L214-L216 是安全网——若基线 `J_at_zero` 更小,强制返回 (0,0)。
- [methods/jointfix.py:L218-L223](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/methods/jointfix.py#L218-L223) —— 返回 `(smooth_scale, trace)`,trace 里带 `a/b/J_final/a_stage1/b_stage1/J_stage1/t_stage1_s/t_stage2_s/n_stage2_candidates/J_at_zero/search_skipped`,最终会被写进 `joint_search_traces.json`。

外层入口 `joint_grid_search_ab` 负责"组装闭包":

- [methods/jointfix.py:L124-L147](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/methods/jointfix.py#L124-L147) —— 从统计字典取 `amax` 与 `Y_refs`,定义标量版 `J`(逐个评估)与批量版 `J_batch`(把 a、b 列表转成张量,一次算完);若目标不是 output-recon 或缺 Y_refs,直接抛 `NotImplementedError`(weight-error 路径未实现)。

**阅读提示(诚实读码)**:`warm_start` 参数在 [methods/jointfix.py:L111-L119](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/methods/jointfix.py#L111-L119) 的 docstring 里被描述为坐标下降的种子机制,但检查 `_smooth_pangu.py` 中所有调用点(本讲 4.3 节)会发现**当前 Pangu 路径并未传入 warm_start**,而是每轮完整重搜、靠统计刷新让搜索自动偏向新最优点。这是"接口预留、实现走了另一条路"的典型样例——读 docstring 时务必对照调用点。

#### 4.1.4 代码实践

**实践目标**:在纯 CPU、无需任何模型权重的环境下,亲手复现两阶段 (a,b) 搜索,观察搜索路径与安全网行为。

**操作步骤**(以下为示例代码,保存为 jointfix 目录下的临时脚本,或 `python -c` 分段执行):

```python
# 示例代码:合成一个带离群通道的线性层,跑一次两阶段搜索
import torch
from jointfix.methods.jointfix import JointSearchConfig, joint_grid_search_ab

torch.manual_seed(0)
h_in, h_out, N = 256, 64, 128
W = torch.randn(h_out, h_in) * 0.02
X = torch.randn(N, h_in)
X[:, :8] *= 40.0                       # 人造 8 个离群通道 -> 平滑应找到 a>0

stats = {
    "amax": X.abs().amax(dim=0),       # x_stat: 每通道激活 amax
    "X_sample": X,                     # 目标函数用的采样激活
}
w_stat = W.abs().amax(dim=0)           # w_stat: 每输入通道权重 amax
# output-recon 目标需要 Y_refs(BF16 真值输出),这里直接用 float 真值
from jointfix.methods.smooth_search import compute_output_recon_objective
stats["Y_refs"] = [X @ W.T]

cfg = JointSearchConfig()
s, trace = joint_grid_search_ab([W], stats, w_stat, cfg)
print({k: v for k, v in trace.items() if not torch.is_tensor(v)})
print("s range:", s.min().item(), s.max().item())
```

**需要观察的现象**:

1. trace 中 `a_stage1/b_stage1` 落在反对角线上(\(a+b=1\)),`a/b` 是方框细化后的终值;
2. 因为人造离群,预期最优 \(a\) 偏大(激活侧需要更多平滑),`J_final < J_at_zero`;
3. `n_stage2_candidates` 应为 25(5×5 方框);
4. 把 `X[:, :8] *= 40.0` 改成 `X[:, :8] *= 1.0`(无离群),重跑后大概率触发安全网:`a=b=0`、`J_final ≈ J_at_zero`。

**预期结果**:第一组实验搜到非零 (a,b) 且误差低于基线;第二组实验回到 (0,0)。具体数值待本地验证(依赖随机种子与库版本)。

#### 4.1.5 小练习与答案

**练习 1**:为什么 Stage 1 用反对角线(\(a+b=1\))网格而不是全平面均匀撒点?

**答案**:(1) 9 个点的反对角线覆盖了"激活侧/权重侧分担比例"从 1:9 到 9:1 的全部主要形态,而 \(s_c = x_c^a/w_c^b\) 中 \(a\)、\(b\) 同时放大只会让 \(s\) 整体缩放(收益有限但风险高);(2) 预算受控——Stage 1 只花 9 次批量评估,把精细搜索留给 Stage 2 的局部方框;(3) 两阶段"先粗后细"总评估次数 9+25=34,远小于全平面细网格。

**练习 2**:`make_smooth_scale` 里对 x_stat 和 w_stat 都做了 `clamp(min=eps)`,去掉会发生什么?

**答案**:若某通道激活恰好全零(死通道),\(x_c^a = 0^a = 0\) 本身无害,但当 \(b>0\) 且 \(w_c=0\) 时分母为 0 会产生 inf;更危险的是 \(x_c=0\) 且未来引入负指数时出现除零。clamp 保证 \(s\) 恒为有限正数,下游 `X/s`、`W*s` 数值稳定。

**练习 3**:安全网为什么有必要?网格里明明已经有很多候选。

**答案**:网格点都不等于"不平滑"。若校准分布本身没有离群(或该层已被上游平滑"顺带"治好),任何非平 \(s\) 都只会引入额外的量化扰动,此时最优解就是 \(s \equiv 1\),即 (a,b)=(0,0)。安全网拿基线 `J(0,0)` 兜底,保证搜索单调不劣化——这也是量化这种"逐层叠加误差"的流程里非常重要的工程保险。

### 4.2 输出重建目标(output-recon)

#### 4.2.1 概念说明

搜 (a,b) 需要一个"打分函数" \(J(a,b)\)。两种候选:

- **权重误差(weight-error)**:度量 \(\|\hat{W} - W\|^2\)(逐通道加权的 dW²)。便宜,但"权重离得近"不等于"层输出离得近"——不同通道的权重误差对输出的影响权重不同。
- **输出重建(output-recon)**:直接度量量化前后**这一层输出**的差异:

\[
J(a,b) \;=\; \sum_{i} \operatorname{mean}\Big( \big( Q(X/s)\, Q(W_i \cdot s)^{\top} \;-\; Y^{ref}_i \big)^2 \Big), \qquad Y^{ref}_i = X\, W_i^{\top}
\]

其中 \(Q(\cdot)\) 是 INT8 伪量化。真值 \(Y^{ref}\) 用原始 BF16 权重(转 float)计算,搜索要找的是"量化后前向结果最接近不量化的前向结果"的 (a,b)——这正是部署后模型行为的直接代理。

两个实现细节值得注意:

1. **s 因子自动消去**:\(Q\) 返回的是反量化后的值,所以 \(Q(X/s) \cdot Q(W \cdot s)^{\top} \approx X W^{\top}\),目标函数里不需要再手动乘除 \(s\)。
2. **支持多个权重共享一个输入**:MoE 的 gate_proj/up_proj(或一组专家)共享同一个 norm 输出,搜索时必须让所有消费同一激活的权重一起评估——\(\sum_i\) 就是对这组权重求和。

#### 4.2.2 核心流程

```text
单个 (a,b):
  s = x_stat^a / w_stat^b
  X_q = fakeq(X_sample / s)                # 模拟激活 INT8(per-token)
  对组内每个权重 W_i:
      W_q = fakeq(W_i * s)                 # 模拟权重 INT8(per-channel)
      累加 mean( (X_q @ W_q^T - Y_ref_i)^2 )

批量 B 个 (a,b)(性能关键路径):
  在对数域一次构造 B 组 s: log_s = a·log(x) - b·log(w); s = exp(log_s)
  按 ab_chunk(默认 9)分块,块内用广播 + bmm 一次算完 B 个目标值

分布式 B 个 (a,b):
  权重按设备分组,各设备算自己组的部分 MSE,最后 CPU 上做一次跨设备求和
```

#### 4.2.3 源码精读

- [methods/smooth_search.py:L125-L146](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/methods/smooth_search.py#L125-L146) —— `compute_output_recon_objective`,标量版目标函数。docstring 写明"s 因子自动消去"的伪量化语义;逐权重循环累加 MSE。
- [methods/smooth_search.py:L149-L197](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/methods/smooth_search.py#L149-L197) —— 批量版。L169-L174 是性能精髓:先取对数,把 B 组 \(s\) 的构造变成一次 broadcast 乘加(`log_s = log_x·a − log_w·b`),再 `exp` 回来;随后按 `ab_chunk` 分块,块内 `int8_fake_quantize` 与 `torch.bmm` 全部向量化。docstring 声明与标量版语义等价(有测试断言)。
- [methods/smooth_search.py:L200-L251](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/methods/smooth_search.py#L200-L251) —— 分布式版:每台设备只算自己分到的权重子集的部分 MSE(每个候选只需一次跨设备规约,而不是 B 次),部分和在 CPU 上累加;同样有等价性测试。
- [methods/smooth_search.py:L48-L56](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/methods/smooth_search.py#L48-L56) —— Hessian 对角代理的通道加权 \(w_c = (E[x^2]_c \cdot E[w^2]_c / \text{mean})^{\alpha}\),以及 [L40-L45](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/methods/smooth_search.py#L40-L45) 的旧版 outlier 启发式加权。

**阅读提示(文档 vs 代码)**:文档 [docs/quantize_openpangu_w8a8.md:L7](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/docs/quantize_openpangu_w8a8.md#L7) 说方法"采用 Hessian 通道加权"。核对代码会发现:通道加权只被 weight-error 目标消费,而 [methods/jointfix.py:L87-L100](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/methods/jointfix.py#L87-L100) 的 `stats_config()` 明确写着——默认 objective=output-recon 时**既不收集直方图也不收集矩**(通道加权所需统计根本不采集),且 [L126-L129](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/methods/jointfix.py#L126-L129) 对 weight-error 直接抛未实现。**默认配置下真正落地的"Hessian 加权"在 4.4 节的 GPTQ 里**(它使用完整 Hessian 矩阵 \(H = X^\top X / N\),见 [core/primitives.py:L134-L136](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/core/primitives.py#L134-L136))。读工具类项目时,"文档描述的能力"与"当前默认路径实际执行的能力"要分开核对。

#### 4.2.4 代码实践

**实践目标**:验证"批量版与标量版语义等价"这一 docstring 声明,并直观感受批量版的速度收益。

**操作步骤**(示例代码):

```python
# 示例代码:等价性 + 计时对比
import time, torch
from jointfix.methods.smooth_search import (
    compute_output_recon_objective, compute_output_recon_objective_batched_ab)

torch.manual_seed(0)
weights = [torch.randn(128, 512) * 0.02 for _ in range(4)]
X = torch.randn(1024, 512); X[:, :16] *= 30
Y_refs = [X @ W.T for W in weights]
x_stat, w_stat = X.abs().amax(dim=0), weights[0].abs().amax(dim=0)

a_list, b_list = zip(*[(i / 10, 1 - i / 10) for i in range(1, 10)])

t0 = time.time()
scalar = [compute_output_recon_objective(weights, Y_refs, X, x_stat, w_stat, a, b)
          for a, b in zip(a_list, b_list)]
t1 = time.time()
batched = compute_output_recon_objective_batched_ab(
    weights, Y_refs, X, x_stat, w_stat,
    torch.tensor(a_list), torch.tensor(b_list)).tolist()
t2 = time.time()
print("max diff:", max(abs(p - q) for p, q in zip(scalar, batched)))
print(f"scalar {t1-t0:.3f}s vs batched {t2-t1:.3f}s")
```

**需要观察的现象**:两组 J 值最大差异应在浮点舍入量级(远小于 1e-5);批量版耗时显著更低。

**预期结果**:等价性成立、批量版快一个数量级左右。精确倍数待本地验证。

#### 4.2.5 小练习与答案

**练习 1**:为什么 Y_refs 由调用方(`_precompute_Y_ref`)预计算并塞进 stats,而不是在目标函数里现算?

**答案**:Y_ref = X @ W^T 与 (a,b) 无关,在整个搜索(9+25 个候选、K 轮迭代)中恒定。预计算一次、反复只读,把每次评估的成本从"两次大矩阵乘"降为"一次伪量化 + 一次 bmm";分布式版还把各设备的 Y_refs 常驻在对应设备上,避免跨设备搬运。

**练习 2**:伪量化 `int8_fake_quantize`([core/primitives.py:L53-L72](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/core/primitives.py#L53-L72))对权重和激活分别按什么粒度模拟?为什么搜索阶段用它而不是真的转 int8?

**答案**:它把输入 reshape 成 [rows, cols] 后**按最后一维(行)逐行**求 scale——对 [h_out, h_in] 的权重即 per-output-channel,对 [tokens, hidden] 的激活即 per-token,恰好精确对应 W8A8 的两侧粒度。用"反量化后的 float"模拟 INT8,是为了让 J 可以反向比较误差、可以批量向量化,且不必关心 int8 的存储布局;真正落盘 int8 发生在 4.4 节的写出阶段。

**练习 3**:如果 MoE 层搜 gate/up 平滑时只把 `gate_proj` 放进权重组、漏掉同输入的 `up_proj` 与 256 个专家,会发生什么?

**答案**:J 只反映 gate_proj 一家的输出误差,搜出的 (a,b) 对它最优、对被漏掉的权重可能很差;且平滑落地时 norm 输出被同一 \(s\) 缩放,所有消费方都被动接受这个次优 \(s\),量化误差在整层累积。这正是 [methods/_smooth_pangu.py:L156-L166](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/methods/_smooth_pangu.py#L156-L166) 把 gate/up × (mlp、shared_experts、全部路由专家) 全部收进 `gate_up_keys` 再一起搜的原因。

### 4.3 Pangu 吸收拓扑与 K=2 坐标下降

#### 4.3.1 概念说明

搜出 \(s\) 之后,变换 \((X \oslash s)(W \odot s)^\top\) 必须在**部署图里零成本**实现——不能在推理时多出一个除法算子。做法是把 \(\div s\) **吸收(absorb)进上游参数**、把 \(\times s\) 吸收进本层权重,推理路径完全不变形。`_smooth_pangu.py` 的模块 docstring 自称"这个文件就是 Pangu 特有的吸收拓扑",包含四个吸收点:

| 平滑对象 | ÷s 吸收进 | ×s 补偿对象 | 备注 |
|----------|-----------|-------------|------|
| q_b_proj | `q_a_layernorm.weight` | `indexer.wq_b`(不量化但同输入) | MLA 低秩侧 |
| o_proj | `kv_b_proj` 的 V 行(按头交错切片) | — | 每头 VHD 行独立除 |
| gate/up(全部专家) | `pre_mlp_layernorm.weight` | 路由器 `mlp.gate`(**漏掉曾致约 40% 路由翻转**)、未被搜索的共享专家 gate/up |
| down_proj | `up_proj` 的行 | — | down 的输入通道 = up 的输出维 |

**坐标下降的必要性**:gate/up 的 \(s_{gu}\) 除在 norm 输出上,改变了 down 的输入;down 的 \(s_{down}\) 除在 up 的输出行上,又改变了 gate/up 想要压平的对象。两组参数耦合,一次各自独立求解并非全局最优,于是交替:固定 \(s_{down}\) 搜 \(s_{gu}\) → 落地 \(s_{gu}\) 并**解析刷新**受影响统计 → 搜 \(s_{down}\) → 再刷新 → 进入下一轮,直到 (a,b) 变化小于容差或轮数用尽。

**解析刷新**是本模块最漂亮的工程点:平滑施加后,下游线性层看到的激活变为 \(x_{new} = x / s\),而 amax、分位数、采样矩阵都对 \(s\) **线性缩放**、\(E[x^2]\) 按 \(s^2\) 缩放,所以无需重跑校准前向,一次除法就能得到与重新收集完全一致的统计([core/stats.py:L166-L185](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/core/stats.py#L166-L185) 的 docstring 明确断言数学等价)。

**路由专家的处理**:单设备路径对每个专家**各自搜索** (a,b),但落地时全体统一用**中位数** (a,b)(每专家的 \(s\) 仍由各自的 x/w 统计算出);多设备路径则保留逐专家 (a,b)。模块 docstring([methods/_smooth_pangu.py:L10-L18](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/methods/_smooth_pangu.py#L10-L18))把这一差异标注为"per-expert (a,b) 只存在于分布式路径"。

#### 4.3.2 核心流程

一个 MoE decoder 层的完整处理(`smooth_quantize_pangu_layer`):

```text
输入: layer_tensors(该层原始 BF16 权重), collectors(各采集点激活统计),
      skip_patterns, JointSearchConfig, device/devices
1. 注意段(若未 skip):
   1a. q_b_proj 组: finalize 统计 → w_stat=列amax → 搜 (a,b) →
       q_a_layernorm /= s; q_b_proj *= s; indexer.wq_b *= s(补偿)
   1b. o_proj 组:   搜 (a,b) → o_proj *= s; 对每个头 h:
       kv_b_proj 的 V 行区间 [v_start,v_end) /= s[h*VHD:(h+1)*VHD]
2. K 轮坐标下降(K = max(1, num_iterations), 生产推荐 2):
   2a. gate+up 组: 收集 mlp/shared/experts 全部 gate_proj、up_proj 为
       gate_up_keys;w_stat 取堆叠 amax 的逐元素最大(跨权重联合);
       多设备把权重轮转分组、走分布式搜索;落地: norm /= s, 全组 *= s,
       未入组的共享专家 gate/up *= s, mlp.gate *= s(路由补偿)
   2b. down 组(dense/shared 各一 + 每个路由专家各一):
       搜 (a,b) → up_proj 行 /= s; down_proj *= s;
       若还有下一轮: refresh_stats_after_smooth 把该采集点统计除以 s
   2c. 收敛判定: 本轮所有 (a,b) 与上轮差异 ≤ iter_ab_tol → 提前退出
3. 写出(见 4.4)
```

#### 4.3.3 源码精读

先看入口契约与统计键约定:

- [methods/_smooth_pangu.py:L55-L81](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/methods/_smooth_pangu.py#L55-L81) —— 函数签名与 docstring:返回 `(out_tensors, n_smooth, n_quantized, traces)`;`devices` 参数决定单/多设备行为(>1 时 gate+up 分布式搜索、路由专家获得逐专家 (a,b))。
- [methods/_smooth_pangu.py:L16-L18](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/methods/_smooth_pangu.py#L16-L18) —— 采集点键名约定(`{pfx}.q_b_in / o_in / mlp_in / exp{eid}_down_in / shared_down_in / dense_down_in`),由 Pangu 后端的统计钩子按同名写入,本函数按键取用。
- [methods/_smooth_pangu.py:L35-L47](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/methods/_smooth_pangu.py#L35-L47) —— 两个小工具:`_precompute_Y_ref`(BF16 真值输出)与 `_ab_converged`(所有共享键的 (a,b) 位移都 ≤ tol 才算收敛)。

注意段两个吸收点:

- [methods/_smooth_pangu.py:L90-L108](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/methods/_smooth_pangu.py#L90-L108) —— q_b_proj 组:搜索、落地三步(norm ÷、权重 ×),L106-L108 是 indexer.wq_b 的**不量化补偿**——它是 DSA Indexer 的输入侧、留在 BF16,但因为消费同一个 norm 输出,必须同样乘 \(s\) 才能保持自身前向不变。
- [methods/_smooth_pangu.py:L110-L134](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/methods/_smooth_pangu.py#L110-L134) —— o_proj 组:L129-L133 的按头循环是"head-interleaved V 行"切片——kv_b_proj 每头输出布局为 [nope 维 + v 维],o_proj 的输入通道按 (头, 头内 V 维) 排列,所以把 \(s\) 的第 h 段除到该头对应的 V 行区间上,`NOPE/VHD` 等维度全部来自模型 config。

坐标下降主体:

- [methods/_smooth_pangu.py:L136-L146](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/methods/_smooth_pangu.py#L136-L146) —— K 轮循环骨架与 `_get_stats` 缓存(每采集点只 finalize 一次,后续读缓存;迭代间缓存会被刷新替换)。
- [methods/_smooth_pangu.py:L149-L199](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/methods/_smooth_pangu.py#L149-L199) —— gate+up 组:L156-L166 收集全部消费方(`mlp`、`mlp.shared_experts`、`experts.{eid}` 的 gate/up_proj 且未被 skip);L171-L177 多设备把权重轮转分发到各卡做分布式搜索,单设备则在 L179-L183 用堆叠 amax 的逐元素最大作为联合 w_stat、逐权重构造 Y_refs;L186-L199 落地——norm ÷、全组 ×、共享专家补偿 ×、**路由器补偿 ×**(L196-L199 注释原文:漏掉这项曾造成约 40% 路由翻转,因为路由器读的也是 pre_mlp_layernorm 输出,不补偿的话它看到的是被 \(s\) 缩放后的激活,打分分布整体漂移)。
- [methods/_smooth_pangu.py:L201-L230](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/methods/_smooth_pangu.py#L201-L230) —— `_smooth_down` 闭包:dense 与 shared 各调用一次;L221-L222 落地方向是 **up_proj 的行 ÷s(unsqueeze(1) 按输出维)、down_proj 列 ×s**;L223-L224 是迭代衔接关键——若还有下一轮,调用 `refresh_stats_after_smooth` 把该采集点统计解析缩放。
- [core/stats.py:L166-L185](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/core/stats.py#L166-L185) —— 解析刷新实现:amax/p99_9/median/X_sample 除以 \(s\)、\(E[x^2]\) 除以 \(s^2\),docstring 说明这与重跑校准前向数学等价——"免费"拿到下一轮的准确统计。
- [methods/_smooth_pangu.py:L232-L307](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/methods/_smooth_pangu.py#L232-L307) —— 路由专家:L242-L279 每专家独立搜(多设备时线程池轮转分卡、先预热统计缓存防竞争);L281-L293 单设备取全体 (a,b) 的**中位数**、多设备逐专家保留;L295-L307 用(共享或专属的)(a,b) 配合**各专家自己的 x/w 统计**构造 \(s\) 落地,同样在迭代间刷新统计。
- [methods/_smooth_pangu.py:L309-L314](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/methods/_smooth_pangu.py#L309-L314) —— 收敛早退:第二轮起,若所有 (a,b) 位移 ≤ `iter_ab_tol` 则 break,省掉余下轮次。

最后,runner 如何调用这一切:

- [core/runner.py:L182](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/core/runner.py#L182) —— 逐层循环里 `method.process_layer(weights, collectors, spec, backend, device, devices)` 被调用(即 u8-l2 主循环的"量化"步骤)。
- [core/runner.py:L209-L210](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/core/runner.py#L209-L210) —— 层循环结束后 `method.dump_traces(out_dir)`,把本讲搜到的每个 (a,b) 写成 `joint_search_traces.json`。

方法侧入口(`JointFixMethod.process_layer`)只做委托与 skip 拼接:

- [methods/jointfix.py:L312-L327](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/methods/jointfix.py#L312-L327) —— 惰性 import 打破循环依赖(jointfix → _smooth_pangu → jointfix);L319-L321 把后端 skip 模式与 `skip_shared_experts` 追加的 `"mlp.shared_experts"` 拼起来传入;traces 存到 `self._traces` 供 `dump_traces` 落盘。
- [methods/jointfix.py:L329-L343](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/methods/jointfix.py#L329-L343) —— `dump_traces` 剔除张量字段只留标量(a、b、J 等),写 `joint_search_traces.json`,与单体脚本产物同构以便 `tools/compare_joint_traces.py` 对拍。

接口契约在基类里:

- [methods/base.py:L50-L76](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/methods/base.py#L50-L76) —— `process_layer` 抽象方法的完整契约:输入"该层原始权重字典 + 激活统计 collectors",输出"可直接交给 `backend.save_quantized` 的张量字典"(int8 权重 + bf16 scale + 透传 BF16)。docstring 强调方法操作的是**原始权重字典**而非 nn.Module——建好的模块只服务于校准前向。

#### 4.3.4 代码实践

**实践目标**:从一次真实 `jointfix quantize` 运行的产物 `joint_search_traces.json` 中,读取并解读一个 MoE 层的坐标下降轨迹(无法跑真机时的替代:阅读源码推演字段)。

**操作步骤**:

1. 按 [docs/quantize_openpangu_w8a8.md:L31-L42](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/docs/quantize_openpangu_w8a8.md#L31-L42) 的命令执行量化(关键开关 `--num-iterations 2 --iter-ab-tol 0.05`);
2. 打开输出目录里的 `joint_search_traces.json`,任选一个 MoE 层的 `mlp.down_proj.weight` 与某个 `exp{eid}.down_proj.weight` 条目;
3. 记录 `a_stage1/b_stage1 → a/b` 的变化,以及 `J_final` 与 `J_at_zero` 的比值。

**需要观察的现象**:

- `J_at_zero`(不平滑基线)明显大于 `J_final` 的层,通常就是离群严重的层;
- 路由专家条目的 (a,b) 在单设备路径下应聚集在中位数附近(落地统一用中位数),`--num-devices 16` 时则各专家彼此发散;
- 若第二轮就收敛,日志里不会出现第三轮搜索耗时(trace 的 `t_stage1_s/t_stage2_s` 只记最后一轮)。

**预期结果**:`J_final/J_at_zero` 普遍小于 1(平滑有效);专家 (a,b) 的离散度在单卡与 16 卡运行之间有肉眼可见差异。真机数值待本地验证。

#### 4.3.5 小练习与答案

**练习 1**:为什么 gate/up 搜索的 w_stat 用"堆叠后逐元素取最大"而不是平均?

**答案**:w_stat 决定 \(s\) 的权重侧形状,而 \(s\) 会被**同一组**所有权重共享。若用平均,某个专家在某通道上的大幅值会被其他专家稀释,导致该通道 \(s\) 偏小、该专家权重侧量化误差偏大;取逐元素最大是保守策略——按"最难的消费者"定形状,保证没有任何一家权重拿到超出其承受力的缩放。

**练习 2**:路由器 `mlp.gate` 不被量化(命中 `mlp.gate.` skip 模式),为什么平滑后还要乘 \(s\)?

**答案**:平滑把 `pre_mlp_layernorm.weight` 除以了 \(s\),该 norm 的输出整体变为原来的 \(1/s\)。路由器与 gate/up 消费**同一个** norm 输出,若不把路由器权重乘回 \(s\),它的输入分布被整体缩放,sigmoid 打分随之漂移,专家选择会翻转——源码注释记录这个教训的量级约为 40% 路由翻转([methods/_smooth_pangu.py:L195-L199](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/methods/_smooth_pangu.py#L195-L199))。同理,indexer.wq_b 也要补偿。

**练习 3**:`num_iterations=1`(代码默认)时,K 轮坐标下降退化成什么?此时 `refresh_stats_after_smooth` 还会被调用吗?

**答案**:K=1 时循环只跑一轮:gate/up 与 down 各搜一次、各落地一次,顺序执行、无交替,退化为"一次性独立求解"。看 [methods/_smooth_pangu.py:L223](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/methods/_smooth_pangu.py#L223) 与 [L305](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/methods/_smooth_pangu.py#L305) 的条件 `_it < K - 1`:最后一轮(也是唯一一轮)不再刷新统计,所以 K=1 时刷新完全不执行——这正是生产文档推荐 `--num-iterations 2` 的原因:让两组平滑参数真正"互相看见"一次。

### 4.4 混合量化写出:输出侧 GPTQ + 输入侧 RTN

#### 4.4.1 概念说明

搜索与平滑都完成后,`smooth_quantize_pangu_layer` 的第 4 段把权重真正落成 INT8。这里有一个明确的双轨策略:

- **输出侧(write end)**:`o_proj`、`down_proj`(dense、shared、每个路由专家)——它们把结果写回残差流,误差直接进入后续所有层的输入。这些权重用 **GPTQ** 量化:利用平滑后输入样本 \(X/s\) 的 Hessian \(H = X^\top X / N\) 做逐列误差补偿,最小化的是 \(\|X\hat{W}^\top - X W^\top\|^2\),与 4.2 的 output-recon 目标一脉相承。
- **输入侧(其余被量化的线性层)**:`gate_proj`、`up_proj`、`q_b_proj` 等——它们消费已被 per-token 动态量化的激活,平滑已经把离群压平,直接用便宜的 **RTN**。

GPTQ 的直觉:逐列量化时,把已量化列的误差 \(e_i = w_i - \hat{w}_i\) 通过 Hessian 的 Cholesky 逆信息"摊给"未量化列——即量化第 \(i\) 列后,从剩余列中减去 \(e_i \cdot U_{i,j}/U_{i,i}\),使输出误差在二阶意义上最小。这与坐标下降里的"误差转移"思想一致:总量守恒,但把它转移到对输出影响小的方向上。

GPTQ 有三种**静默**回退到 RTN 的情形:校准样本为空(死专家)、样本数 < 输入维度(Hessian 秩亏)、Cholesky 三次加倍阻尼重试仍失败。回退不打印任何日志,判断依据是正向信号——成功时打印 `[GPTQ-RUN]`,所以"`grep -c '\[GPTQ-RUN\]'` 与被量化权重数之差"就是回退计数。

**skip 模式**决定哪些权重根本不量化:core 提供 LLaMA/Qwen 等通用名单(embed、MLA 低秩、lm_head、路由 gate 等),Pangu 后端追加 DSA Indexer 与 MHC 的专属名单;`--skip-shared-experts` 再追加 `"mlp.shared_experts"`。

#### 4.4.2 核心流程

```text
对层内每个权重 name(排序后遍历):
  if should_quantize(name, skip_patterns):
      if name 是 write end 且在 traces 里存有 (s, 采集点):
          X_smooth = X_sample / s            # 平滑后的输入,供 GPTQ 做 Hessian
      else:
          X_smooth = None                    # 输入侧 → 强制 RTN
      (int8_w, scale) = select_write_quantize(W_smoothed, X_smooth,
                                              write_quant, damp, block_size)
      输出 int8_w 与 scale(写到 name.replace(".weight", ".weight_scale"))
  else:
      原样透传(转回原 dtype)
```

#### 4.4.3 源码精读

- [methods/_smooth_pangu.py:L320-L329](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/methods/_smooth_pangu.py#L320-L329) —— `_is_write_end` 的四条判定:o_proj、非专家 down_proj、shared_experts down_proj、专家 down_proj——精确圈出"写回残差流"的集合。
- [methods/_smooth_pangu.py:L331-L350](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/methods/_smooth_pangu.py#L331-L350) —— 写出循环:L333-L339 从 trace 里取回搜索阶段存的 `s` 与采集点键,重构 `X_smooth = X_sample / s`(注意 trace 在搜索阶段就已存好这两样,见 [L219-L220](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/methods/_smooth_pangu.py#L219-L220) 与 [L301-L302](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/methods/_smooth_pangu.py#L301-L302));L342-L345 调用分发函数;L346-L347 产出 `int8 权重 + .weight_scale`;L350 不量化者透传回原 dtype。
- [core/primitives.py:L189-L214](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/core/primitives.py#L189-L214) —— `select_write_quantize`:`write_quant=="rtn"` 或 `X_smooth is None` → RTN;否则 GPTQ。docstring 点明参数由方法的 JointSearchConfig 传入,core 不依赖方法。
- [core/primitives.py:L84-L89](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/core/primitives.py#L84-L89) —— RTN:按输出通道(h_out 方向逐行)对称量化,int8 + bf16 scale。
- [core/primitives.py:L92-L158](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/core/primitives.py#L92-L158) —— GPTQ 主体:L114-L121 三种静默 RTN 回退;L134-L136 构造 \(H = X^\top X / N\) 并加 `damp × diag均值` 阻尼;L140-L153 Cholesky 求逆链(H → L → H⁻¹ → 上三角 Cholesky),失败加倍阻尼重试至多 3 次;L158 打印 `[GPTQ-RUN] {tag} h_out h_in N` 正向信号。
- [core/primitives.py:L160-L186](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/core/primitives.py#L160-L186) —— 分块补偿循环:逐列量化 → 误差 \(e_i = (w_i - \hat{w}_i)/U_{ii}\) → 从块内未量化列减去 \(e_i \cdot U_{i,j}\)(L178-L179)、从块外列整块摊派(L183-L184)。
- [core/primitives.py:L34-L47](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/core/primitives.py#L34-L47) —— `should_quantize`:非 `.weight` 结尾或非二维直接排除,再逐 skip 模式子串匹配。
- [core/primitives.py:L23-L31](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/core/primitives.py#L23-L31) —— 通用 skip 名单(注意 `mlp.gate.` 带尾点,避免误伤 `gate_proj`)。
- [backends/pangu.py:L22-L27](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/backends/pangu.py#L22-L27) 与 [L91-L92](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/backends/pangu.py#L91-L92) —— Pangu 追加名单(`indexer.wk`、`indexer.weights_proj`、`indexer.wq_b`、`mhc_module.phi`)与拼接逻辑。
- [methods/jointfix.py:L319-L321](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/methods/jointfix.py#L319-L321) —— `--skip-shared-experts` 的落地点:向 skip 追加 `"mlp.shared_experts"`,使共享专家的 gate/up/down 全部走 BF16 透传、也不参与平滑搜索(`_sq` 判定失败即不入组)。
- [docs/quantize_openpangu_w8a8.md:L57-L69](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/docs/quantize_openpangu_w8a8.md#L57-L69) —— 生产参数表:`--num-iterations 2`(坐标下降)、`--iter-ab-tol 0.05`、`--write-quant gptq`(残差流方向权重用 GPTQ)、`--skip-shared-experts`(共享专家留 BF16,理由:**该路径每 token 必经,量化误差全局累积**)。

`--skip-shared-experts` 的理由值得展开:路由专家每次只被 top-k 选中、单专家误差只影响被选中的 token 子集;共享专家(common expert)则**对每一个 token 都生效**,它的量化误差无差别累积进所有 token 的残差流——用一份小体量(相对 256 个路由专家)的 BF16 权重,换掉一条全局误差源,性价比极高。

#### 4.4.4 代码实践

**实践目标**:在合成数据上量化同一权重两次(RTN vs GPTQ),验证 GPTQ 的输出误差更小;并统计 skip 模式的命中行为。

**操作步骤**(示例代码):

```python
# 示例代码:RTN vs GPTQ 输出误差对比 + skip 判定
import torch
from jointfix.core.primitives import (
    rtn_quantize, gptq_quantize, should_quantize, UNIVERSAL_SKIP_PATTERNS)

torch.manual_seed(0)
h_in, h_out, N = 512, 256, 2048
W = torch.randn(h_out, h_in) * 0.02
X = torch.randn(N, h_in); X[:, :16] *= 25     # 离群通道
Y_ref = X @ W.T

q_rtn, s_rtn = rtn_quantize(W)
q_gptq, s_gptq = gptq_quantize(W, X, damp=0.01, block_size=128, tag="demo")
for tag, q, s in [("rtn", q_rtn, s_rtn), ("gptq", q_gptq, s_gptq)]:
    err = ((X @ (q.float() * s.float()).T) - Y_ref).pow(2).mean()
    print(f"{tag}: output MSE = {err.item():.6f}")

names = ["model.layers.0.mlp.gate.weight", "model.layers.0.mlp.gate_proj.weight",
         "model.layers.0.mlp.shared_experts.down_proj.weight"]
t = torch.randn(4, 4)
for n in names:
    print(n, "->", should_quantize(n, t, UNIVERSAL_SKIP_PATTERNS + ["mlp.shared_experts"]))
```

**需要观察的现象**:

1. `[GPTQ-RUN] demo h_out=256 h_in=512 N=2048` 打印出现;
2. gptq 的输出 MSE 小于 rtn(离群越重差距越大);
3. skip 判定:`mlp.gate.weight` → False(命中 `mlp.gate.`)、`mlp.gate_proj.weight` → True(尾点避免误伤)、`mlp.shared_experts.down_proj.weight` → False(命中追加模式)。

**预期结果**:GPTQ 误差低于 RTN,三者判定如上。精确倍数待本地验证。

#### 4.4.5 小练习与答案

**练习 1**:为什么 `mlp.gate.` 的 skip 模式必须带尾点?

**答案**:子串匹配是无位置的。不带尾点的话 `mlp.gate_proj.weight`、`mlp.gate_up_proj` 等名字里也包含 `mlp.gate`,会把本该量化的投影层误伤成 BF16 透传,白白损失压缩率。尾点把匹配收紧到"名为 gate 的子模块"。

**练习 2**:GPTQ 的阻尼(damping)项 \(H_{ii} \mathrel{+}= \lambda \cdot \text{mean}(H_{ii})\) 起什么作用?为什么失败时加倍重试?

**答案**:校准样本有限时 \(H = X^\top X/N\) 常常接近奇异(样本协方差秩亏),Cholesky 分解会数值失败。阻尼把对角线抬高一点等价于岭回归式正则,让分解稳定;\(\lambda\) 太小不足以救活分解、太大则误差补偿信息被冲淡,所以"失败→加倍→再试"是在两者之间自动搜索可分解的最小阻尼。三次仍失败说明 Hessian 病态到不可救,静默退 RTN 保住流程。

**练习 3**:写出阶段如何区分"GPTQ 真跑了"与"静默回退 RTN"?为什么把信号设计成正向打印而不是回退警告?

**答案**:成功路径打印 `[GPTQ-RUN] tag ...`,所以 `grep -c '\[GPTQ-RUN\]' 日志` 与"write end 且有 X_smooth 的权重数"之差即回退数。设计成正向信号是因为大 MoE 层每个专家都可能回退,逐条警告是纯日志噪音([core/primitives.py:L115-L117](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/core/primitives.py#L115-L117) 的注释原文);需要审计时 grep 一个稳定前缀,比在数千行警告里翻找信息效率高得多。

## 5. 综合实践

**任务:对一个真实 decoder 层完成冒烟验证,并解读其量化路径(对应本讲规格中的实践任务)。**

**步骤一——单层冒烟(需要真机与 Pangu checkpoint)**:

按脚本自带用法执行([examples/smoke_pangu_layer.py:L8-L9](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/examples/smoke_pangu_layer.py#L8-L9)):

```bash
cd tools/quant/jointfix
PYTHONPATH=. python examples/smoke_pangu_layer.py \
    --model /path/to/pangu_92B --layer 0 --seq 64 --device npu
```

**先读清楚这个脚本验证什么**——docstring([L2-L13](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/examples/smoke_pangu_layer.py#L2-L13))写明:**它不含任何量化**,只验证 Pangu 后端"load_layer_weights → build_layer → forward"这条接缝(runner 依赖的接口)。执行链路:

- [L36-L38](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/examples/smoke_pangu_layer.py#L36-L38):取第 N 层 LayerSpec,打印 `is_moe` 与附加信息;
- [L40-L42](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/examples/smoke_pangu_layer.py#L40-L42):加载该层真实权重并抽样打印形状;
- [L45-L54](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/examples/smoke_pangu_layer.py#L45-L54):meta → 设备建层,随机 token id 经 embedding 得 hidden,跑一次前向;
- [L56-L62](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/examples/smoke_pangu_layer.py#L56-L62):断言输出有限,打印 mean/std/absmax;
- [L66-L74](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/examples/smoke_pangu_layer.py#L66-L74):**重入校验**——把输出再喂回本层,模拟 runner 里第 N+1 层的接续,断言形状稳定且有限(注释解释了 MHC 流的 4D/3D 两种形态都由 `_flatten_hidden_states` 消化);
- 预期结尾打印 `SMOKE PASS`([L78](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/examples/smoke_pangu_layer.py#L78)),若出现 NaN 或形状错,说明后端建层/前向接缝有问题,必须先修再谈量化。

**因此,想看"(a,b) 搜索过程与量化前后误差"不能靠这个脚本**,对应路径是:跑一次 4.3.4 的 `jointfix quantize` 并解读 `joint_search_traces.json`(a/b/J 轨迹),加上 4.2.4 / 4.4.4 的合成实验(伪量化前后 MSE 对比)。这也正是本讲把三个小实验分散在三个模块、再在此汇总的原因。

**步骤二——手工推演一层 MoE 的量化清单**:

对该层权重逐个标注处理方式,填入下表(以 `model.layers.L` 为前缀):

| 权重 | 平滑? | 吸收点 | 写出 |
|------|-------|--------|------|
| `self_attn.q_b_proj.weight` | 搜 (a,b) | q_a_layernorm ÷ + indexer.wq_b × | RTN |
| `self_attn.o_proj.weight` | 搜 (a,b) | kv_b_proj V 行 ÷(按头) | **GPTQ** |
| `mlp.gate_proj/up_proj`(全部专家) | 联合搜 (a,b) | pre_mlp_layernorm ÷ + mlp.gate × | RTN |
| `mlp.down_proj`(每个专家) | 搜 (a,b)(单卡落地用中位数) | up_proj 行 ÷ | **GPTQ** |
| `mlp.gate.weight` | 补偿 ×s,不搜 | — | BF16 透传 |
| `mlp.shared_experts.*`(加 `--skip-shared-experts`) | 不搜 | gate/up 仍 ×s 补偿 | BF16 透传 |
| `indexer.wq_b/wk/weights_proj`、`mhc_module.phi` | wq_b 仅补偿 ×s | — | BF16 透传 |

逐行写出依据(对应 4.3/4.4 精读的哪个函数),最后用 `--skip-shared-experts` 的源码落地点([methods/jointfix.py:L319-L321](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/methods/jointfix.py#L319-L321))与文档理由([docs/quantize_openpangu_w8a8.md:L67](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/docs/quantize_openpangu_w8a8.md#L67):共享专家每 token 必经、误差全局累积)回答"为什么要留 BF16"。

## 6. 本讲小结

- **平滑是数学等价变换**:\((X \oslash s)(W \odot s)^\top = X W^\top\) 精确成立;联合因子 \(s_c = x_c^a / w_c^b\) 把"量化难度"在激活侧与权重侧之间二维分配,两阶段搜索(反对角 9 点粗扫 + 5×5 方框细搜)配安全网(永不劣于不平滑)。
- **目标函数是输出重建**:J(a,b) 直接度量伪量化前向与 BF16 真值的 MSE,批量版在对数域一次构造 B 组 s;文档所说的"Hessian 通道加权"在默认 output-recon 路径下并不读取,真正跑的 Hessian 在 GPTQ 的 \(H = X^\top X/N\) 里。
- **吸收拓扑让平滑零推理成本**:四个吸收点(q_a_layernorm、kv_b_proj V 行、pre_mlp_layernorm、up_proj 行),所有同输入但不量化的消费者(indexer.wq_b、路由器 mlp.gate)必须补偿 ×s——漏掉路由器曾造成约 40% 路由翻转。
- **K=2 坐标下降 + 统计解析刷新**:gate/up 与 down 的平滑参数耦合,交替搜索;`refresh_stats_after_smooth` 按 amax/s、E[x²]/s² 解析缩放统计,等价于重跑校准前向但零成本;`iter_ab_tol` 内位移即早退。
- **写出双轨**:写回残差流的 o_proj/down_proj 用 GPTQ(Hessian 误差补偿、三种静默 RTN 回退、`[GPTQ-RUN]` 为正向信号),其余输入侧用 RTN;skip 名单 = core 通用 + Pangu 专属 + 可选 shared_experts。
- **`--skip-shared-experts` 的本质**:共享专家每 token 必经、误差全局累积,留 BF16 是花小显存买全局精度。

## 7. 下一步学习建议

- **下一讲 u8-l4(量化产物组装与 INT8 服务部署)**:本讲产出的"int8 权重 + .weight_scale + BF16 透传"如何被 `core/deploy.py` 的 finalize 组装成 compressed-tensors 模型、`quantization_config` 各字段的生成逻辑,以及用 w8a8 ansible 模板把 INT8 权重真正拉成服务——那是本讲所有搜索成果的最终交付形态。
- **回看 u8-l2 的 runner 主循环**:把本讲的 `process_layer` 放回"前向收统计 → 量化 → 重前向验证"的骨架中,理解"重前向验证"为什么能兜住本讲所有平滑/量化决策的正确性。
- **源码延伸阅读**:对照 [methods/smooth_search.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/methods/smooth_search.py) 的 weight-error 项实现,思考"若要启用 weight-error 目标需要补齐哪些统计与调用链";再看 `tools/compare_joint_traces.py`(dump_traces docstring 提到)如何对拍两次运行的 (a,b)。
- **方法对比阅读**:SmoothQuant(固定 0.5/0.5)、AWQ(网格搜 per-channel 缩放)、GPTQ(仅权重侧误差补偿)分别是本讲框架的特例或子件,理解 JointFix = "联合搜索 + 输出重建 + 坐标下降 + 混合写出"的组合创新点在哪。
