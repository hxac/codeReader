# 混淆矩阵、指标体系与 ROC 曲线（u6-l3）

## 1. 本讲目标

学完本讲，你应该能够：

1. 用 `confusion_matrix` 把「准确率」拆解成 TP/FP/TN/FN 四种结局，并用 `matshow` 画出热图。
2. 说清楚查准率（precision）、查全率（recall）、F1、MCC 各自的公式与适用场景，掌握 `precision_score`/`f1_score` 中 `average='micro'/'macro'/'weighted'` 三种平均方式的差别。
3. 理解 ROC 曲线是如何「扫过所有阈值」生成的，会用 `roc_curve` 三步法画出曲线并用 `auc` 计算曲线下面积。
4. 认识两个工程上最容易踩的坑：
   - 类别不平衡时准确率严重误导（全预测多数类也能拿到 ~90% 准确率，但 F1 = 0）；
   - 指标的「正类」方向必须与少数类对齐——默认 `pos_label=1` 可能 silently 算错了类。

本讲全部代码来自 [ch06/ch06.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py) 的「Looking at different performance evaluation metrics」与「Dealing with class imbalance」两节（第 485–713 行）。

## 2. 前置知识

**准确率及其盲区。** 前 two 讲（u6-l1、u6-l2）里我们一直用 `scoring='accuracy'` 评估模型：准确率 = 判对样本数 / 总样本数。它把「判对」笼统地混在一起，不区分错在哪一类。对类别不平衡的数据（比如 90% 阴性、10% 阳性的体检场景），一个「无脑全预测阴性」的傻瓜模型也能拿到 90% 准确率——但它在关心的阳性类上一个都没抓到。本讲要做的第一件事就是把准确率「拆开看」。

**二分类的四种结局。** 把「真实类别」和「预测类别」交叉，就得到四个格子：

- **TP**（True Positive，真阳性）：真实是正类，预测也是正类；
- **FP**（False Positive，假阳性）：真实是负类，却预测成正类（误报）；
- **FN**（False Negative，假阴性）：真实是正类，却预测成负类（漏报）；
- **TN**（True Negative，真阴性）：真实是负类，预测也是负类。

注意「正类（positive）」不是「好」的意思，而是「你关心的那个类」。在乳腺癌数据集里，我们关心的是**恶性（malignant）**肿瘤，所以把恶性定为正类。

**决策分数与阈值。** 分类器内部先算出一个连续分数（如 `predict_proba` 给出的概率），再和一个阈值比较才输出类别标签。默认阈值 0.5 只是众多选择之一。**ROC 曲线就是把阈值从最严扫到最松，画出每个阈值下的（假阳性率， 真阳性率）点**。理解「分类器输出的是分数、阈值是可调的」是理解 ROC 的关键。

**承接前两讲。** 本讲直接复用 u6-l1 建立的 `Pipeline`（`StandardScaler` + 分类器）与 u6-l2 的 `GridSearchCV`：先调参得到最优管线，再在**锁定的测试集**上计算各种指标。指标计算永远放在调参之后、测试集之上。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [ch06/ch06.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py) | 本讲主源码。第 485–554 行：混淆矩阵与热图；第 557–595 行：precision/recall/F1/MCC 与自定义 scorer；第 599–662 行：ROC 曲线与 AUC；第 666–713 行：多分类 scorer 与类别不平衡处理 |
| [ch06/wdbc.data](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/wdbc.data) | 乳腺癌 Wisconsin 数据集本地副本（569 行；无表头，第 2 列是 B/M 标签），UCI 在线不可用时的备用读取路径 |
| [ch06/figures/06_09.png](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/figures/06_09.png) | 书中混淆矩阵热图成图（对应本讲 4.1） |
| [ch06/figures/06_10.png](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/figures/06_10.png) | 书中三折交叉验证 ROC 曲线成图（对应本讲 4.3） |

数据与管线的上下文（前两讲已详细讲过，这里只列行号备用）：数据读取与标签编码在 [ch06/ch06.py:102-142](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L102-L142)，SVM 管线与网格搜索在 [ch06/ch06.py:334-363](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L334-L363)。

## 4. 核心概念与源码讲解

### 4.1 混淆矩阵：把准确率拆成四种结局

#### 4.1.1 概念说明

混淆矩阵（confusion matrix）是一张 行=真实类别、列=预测类别 的方阵，第 `(i, j)` 格是「真实为 i、预测为 j」的样本数。它把一个笼统的准确率数字展开成四个格子，于是能回答：错的那几个样本，是误报（把良性判成恶性）还是漏报（把恶性判成良性）？在医疗场景里，漏报（FN）的代价远高于误报（FP），这个信息准确率完全给不出来。

用书中的数据先建立一个全局认知：`wdbc.data` 共 569 个样本，**357 个良性（B）、212 个恶性（M）**——恶性是少数类。`LabelEncoder` 按字母序编码，B→0、M→1，于是**少数类「恶性」恰好落在标签 1，也就是 sklearn 默认的正类**。这正是书中「把少数类/关心的类标为正类」的实践建议：正类对齐少数类后，precision/recall/F1 才都在度量「抓少数类」的能力。

#### 4.1.2 核心流程

```text
读数据 → LabelEncoder 编码（B→0, M→1）→ 分层划分（测试集 114 个样本）
→ 在训练集上网格搜索并 refit 最优 SVC 管线
→ 对测试集 predict
→ confusion_matrix(y_true=y_test, y_pred=y_pred)
→ matshow 画热图 + 双重循环填数字
→ （可选）labels=[1, 0] 重排行列顺序
```

`confusion_matrix` 默认把类别按升序排在两个轴上（本例 0、1）：

```text
              预测 0      预测 1
真实 0      [  TN=71    FP=1  ]
真实 1      [  FN=2     TP=40 ]
```

若希望「真阳性落在左上角」（医学报告的习惯），传 `labels=[1, 0]` 让轴按 1、0 排列，行列同时翻转。

#### 4.1.3 源码精读

**标签编码决定了谁是正类。** 这段是理解后面所有指标方向的前提：

[ch06/ch06.py:124-133](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L124-L133) —— 取第 2 列之后的所有列为特征 `X`、第 1 列（B/M 字符标签）为 `y`，用 `LabelEncoder` 把字符标签编码成 0/1；最后 `le.transform(['M', 'B'])` 显式验证编码方向，输出 `array([1, 0])`，即 M（恶性、少数类）= 1 = 正类。

[ch06/ch06.py:139-142](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L139-L142) —— `train_test_split` 以 `test_size=0.20`、`stratify=y` 分层划分：测试集 114 个样本（72 个类 0 + 42 个类 1），与全局 357:212 的比例一致。

**先训练，再预测，再混淆。** 前两讲已经调好的 SVM 管线（[ch06/ch06.py:334-335](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L334-L335)：`StandardScaler` + `SVC(random_state=1)`）在这里直接复用：

[ch06/ch06.py:498-501](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L498-L501) —— `pipe_svc.fit(X_train, y_train)` 训练、`predict(X_test)` 得到预测标签，然后一行 `confusion_matrix(y_true=y_test, y_pred=y_pred)` 打印 2×2 矩阵。注意参数名 `y_true` 在前、`y_pred` 在后——位置传参时千万别搞反，否则等于评估一个「把模型当真值」的荒谬矩阵。

**热图绘制：`matshow` + 双重循环填字。**

[ch06/ch06.py:506-518](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L506-L518) —— 用 `ax.matshow(confmat, cmap=plt.cm.Blues, alpha=0.3)` 把矩阵画成淡蓝色块，再用 `for i ... for j ...` 双重循环遍历每个格子，`ax.text(x=j, y=i, s=confmat[i, j])` 把具体数字写在色块中央（注意 `matshow` 的 x 对应列 j、y 对应行 i）；`ax.xaxis.set_ticks_position('bottom')` 把刻度挪到底部，最后标注 `Predicted label` / `True label` 两个轴名。成图即 [ch06/figures/06_09.png](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/figures/06_09.png)。

**`labels=[1, 0]` 重排：同一个矩阵，两种视角。**

[ch06/ch06.py:544-554](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L544-L554) —— 书中「Additional Note」：默认顺序下真阴性在左上角 `(0,0)`；传入 `labels=[1, 0]` 后行列同时翻转，真阳性挪到左上、真阴性落到右下。末尾的注释逐格读数（这是书自己给出的标准答案）：**TN=71、FP=1、FN=2、TP=40**——72 个良性里 71 个判对、1 个误报；42 个恶性里 40 个判对、2 个漏报。准确率 = (71+40)/114 ≈ 0.974。

#### 4.1.4 代码实践

1. **实践目标**：亲手产出混淆矩阵与热图，确认四个格子的数值。
2. **操作步骤**：
   - 在 ch06 目录下（或 notebook 中）先执行前文的数据准备与 `pipe_svc` 训练（[ch06/ch06.py:498-501](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L498-L501)）；
   - 打印 `confusion_matrix(y_test, y_pred)`，再打印 `confusion_matrix(y_test, y_pred, labels=[1, 0])`；
   - 运行热图代码（[ch06/ch06.py:506-518](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L506-L518)）。
3. **需要观察的现象**：两种 `labels` 顺序的矩阵行列互换但四个数值不变；热图对角格子（71、40）颜色最深。
4. **预期结果**：默认顺序下矩阵为 `[[71, 1], [2, 40]]`，与书注释一致（71+1+2+40=114，测试集规模吻合）。若你的环境数值不同，先检查是否固定了 `random_state=1`。
5. 具体到小数位的数值为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：若把 `confusion_matrix(y_true=y_pred, y_pred=y_test)` 的两个参数写反，矩阵会变成什么样？

**答案**：参数对调后，`(i, j)` 格的含义变为「预测为 i、真实为 j」，结果等于原矩阵的**转置** `[[71, 2], [1, 40]]`：对角线上的 TP、TN 数值与位置都不变（转置不动对角元），但 FP 与 FN 互换。混淆矩阵一般不对称，FP/FN 一旦混淆，后续所有指标的方向就全错了——这正是必须用参数名显式传 `y_true`、`y_pred` 的原因。

**练习 2**：这个 SVM 有 1 个 FP（良性误判为恶性）和 2 个 FN（恶性漏诊）。从患者角度，哪一种错误更危险？我们希望混淆矩阵的哪个格子尽量小？

**答案**：FN（漏诊）更危险——恶性肿瘤被放走会延误治疗。所以 FN（真实 1、预测 0 的格子，本例为 2）要尽量小，即使为此多几个 FP（多做几次活检确认）也可接受。这正是下一节「查全率优先」的动机。

### 4.2 查准率、查全率、F1、MCC 与 micro/macro/weighted

#### 4.2.1 概念说明

混淆矩阵的四个格子可以组合出一系列「只盯正类」的指标：

- **查准率（precision）**：\( \text{PRE} = \dfrac{TP}{TP + FP} \) ——「模型说是恶性的里面，真是恶性的比例」，衡量**报警准不准**；
- **查全率（recall）**：\( \text{REC} = \dfrac{TP}{TP + FN} \) ——「真是恶性的里面，被模型抓到的比例」，衡量**漏没漏**（也叫 sensitivity/TPR）；
- **F1**：两者的调和平均 \( F1 = 2 \cdot \dfrac{\text{PRE} \times \text{REC}}{\text{PRE} + \text{REC}} = \dfrac{2TP}{2TP + FP + FN} \)。调和平均惩罚「一条腿短」：PRE=1.0、REC=0.1 时算术平均 0.55 看着还行，调和平均只有 ≈0.18，把偏科暴露无遗；
- **MCC**（Matthews 相关系数）：\( \text{MCC} = \dfrac{TP \times TN - FP \times FN}{\sqrt{(TP+FP)(TP+FN)(TN+FP)(TN+FN)}} \)，同时用到全部四个格子，取值 [−1, 1]，被认为是对单一阈值指标最均衡的一个。

**多分类时怎么平均？** 三种 `average` 策略：

| 方式 | 计算规则 | 特点 |
| --- | --- | --- |
| `micro` | 先把所有类的 TP/FP/FN 汇总再算 | 单标签多分类下**恒等于准确率** |
| `macro` | 每个类各算一个 F1，再取不加权平均 | 少数类与多数类**话语权相同**，对不平衡敏感 |
| `weighted` | 每个类的 F1 按其样本量（support）加权平均 | 折中，总体性能的代表 |

**方向陷阱（本讲最重要的坑）**：`precision_score`/`recall_score`/`f1_score` 对二分类默认 `average='binary'`、`pos_label=1`——**只算「标签为 1 的那个类」的指标，且不告诉你算的是哪个类**。本数据集里 LabelEncoder 恰好把少数类恶性编成 1，默认值蒙对了；但如果你的编码顺序相反（少数类是 0），默认调用会静悄悄地去算多数类（良性）的指标，结果看起来依然漂亮，却完全答非所问。修正方式：显式传 `pos_label`（书中示范了 `make_scorer(f1_score, pos_label=0)`），或改用显式指定类别的 `average='macro'`。而**「F1 = 0」的极端情形**是：模型从不预测正类（TP=0 ⇒ PRE=REC=F1 全为 0），此时准确率仍可能高达 90%——见 4.4 的傻瓜分类器。

#### 4.2.2 核心流程

```text
二分类：
  混淆矩阵四格 → PRE / REC / F1 / MCC（默认 pos_label=1）
  ↓ 若关心的正类是 0
  make_scorer(f1_score, pos_label=0) → 塞进 GridSearchCV 的 scoring
      （调参目标与评估目标保持一致）

多分类：
  每类各算指标 → 按 micro / macro / weighted 规则合并成单个数
```

#### 4.2.3 源码精读

**四个指标一次算齐。**

[ch06/ch06.py:562-572](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L562-L572) —— 依次调用 `precision_score`、`recall_score`、`f1_score`、`matthews_corrcoef`，全部只传 `y_true=y_test, y_pred=y_pred`，即沿用默认 `average='binary'`、`pos_label=1`。用 4.1 的矩阵代入公式（40/41、40/42、80/83、以及 MCC 公式），参考值约为 PRE 0.976、REC 0.952、F1 0.964、MCC 0.943（由书注释中的矩阵推得，待本地验证）。这些指标从 [ch06/ch06.py:25-29](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L25-L29) 的 import 引入。

**把指标接到网格搜索上：`make_scorer` 定制打分器。**

[ch06/ch06.py:578-595](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L578-L595) —— `scorer = make_scorer(f1_score, pos_label=0)` 把 `f1_score` 包装成 GridSearchCV 能用的打分器，并显式声明「正类是标签 0」；随后沿用 u6-l2 的双列表 `param_grid`（linear 核扫 `C`，rbf 核扫 `C`×`gamma`）做 10 折网格搜索，`scoring=scorer` 让**选参标准从 accuracy 换成「对指定正类的 F1」**。这就是「你关心什么就优化什么」：调参与评估用同一个指标，避免按准确率选出的模型在 F1 上并非最优。

**多分类 scorer 的完整写法。**

[ch06/ch06.py:670-673](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L670-L673) —— `make_scorer(score_func=precision_score, pos_label=1, greater_is_better=True, average='micro')`：这是多分类场景的模板——`average` 显式指定平均方式（二分类的 `binary` 在多分类下不可用），`greater_is_better=True` 表示分数越大越好（若是损失函数则传 `False`，sklearn 会自动取负号）。`pos_label` 在 micro 下会被忽略，写出来是为了与 binary 模式保持一致的模板习惯。

#### 4.2.4 代码实践

1. **实践目标**：算出三种平均的 F1，并亲手验证「正类方向」对结果的影响。
2. **操作步骤**（接 4.1 实践得到的 `y_test`、`y_pred`）：
   - 运行：`f1_score(y_test, y_pred, average='micro')`、`average='macro'`、`average='weighted'`；
   - 再运行：`f1_score(y_test, y_pred)`（默认，正类=1）与 `f1_score(y_test, y_pred, pos_label=0)`；
   - 对照 4.1 矩阵手算验证。
3. **需要观察的现象**：
   - micro 与 accuracy 相同；
   - 默认（正类=1，少数类恶性）的 F1 **低于** pos_label=0（多数类良性）的 F1——指标在替少数类「说话」，它更难抓；
   - macro 介于两者之间且不受类别大小影响。
4. **预期结果**：由矩阵 `[[71,1],[2,40]]` 推得——micro = 111/114 ≈ 0.974；正类=1 的 F1 = 80/83 ≈ 0.964；正类=0 的 F1 = 142/145 ≈ 0.979；macro ≈ 0.971；weighted ≈ 0.974。若哪一步得到 0，先检查是不是把「从不预测正类的傻瓜预测」代进来了（见 4.4）。精确到小数位的输出为「待本地验证」。
5. 修正演练：假设把 `pos_label=0`（良性）当成了关注对象去调参，等于在优化「多数类表现」。把 4.2.3 的 scorer 换回 `pos_label=1` 重跑网格搜索，对比 `best_params_` 是否变化。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `average='micro'` 的 F1 恒等于准确率？

**答案**：micro 做法是把每个类的 TP 汇总、FP 汇总、FN 汇总再算 F1。对单标签多分类，每个样本恰好预测一个类：全体判对次数 = ΣTP，全体判错次数 = ΣFP = ΣFN。于是 micro-P = micro-R = ΣTP/(ΣTP+ΣFP) = 判对/总数 = 准确率，F1 也就等于它。所以 micro 对不平衡「无感」，不平衡场景应看 macro 或显式正类的 binary 指标。

**练习 2**：F1 为什么用调和平均而不是算术平均？

**答案**：调和平均受较小值支配。PRE=1.0、REC=0.1 时：算术平均 0.55（掩盖短板），调和平均 2×1.0×0.1/1.1 ≈ 0.18（暴露短板）。医疗筛查要求两边都不能塌，调和平均正好惩罚「偏科」的模型。

**练习 3**：书中为什么特意用 `make_scorer(f1_score, pos_label=0)` 演示，而本数据集实际正类是 1？

**答案**：这是**教学上的「方向声明」示范**——告诉你 `pos_label` 是可以（而且应当）显式指定的开关。当你自己的项目里少数类/关心类被编码为 0 时，不写这个参数，默认的 `pos_label=1` 就会算到多数类头上，指标虽高却毫无意义。显式声明让代码意图与指标口径一致。

### 4.3 ROC 曲线与 AUC：扫过所有阈值

#### 4.3.1 概念说明

4.2 的所有指标都基于「阈值固定（predict 已给出 0/1）」。ROC（Receiver Operating Characteristic，受试者工作特征）把阈值松绑：分类器输出连续分数（正类概率），**每个可能的阈值都对应一对（FPR, TPR）**：

- 真阳性率 \( TPR = \dfrac{TP}{TP+FN} \)（= recall，正类里被抓到的比例）；
- 假阳性率 \( FPR = \dfrac{FP}{FP+TN} \)（= 1 − specificity，负类里被误报的比例）。

以 FPR 为横轴、TPR 为纵轴，把所有阈值的点连起来就是 ROC 曲线：

- **对角线**（0,0)→(1,1)：随机猜测，AUC = 0.5；
- **左上角折线**（0,0)→(0,1)→(1,1)：完美分类器，AUC = 1.0——存在一个阈值同时做到 FPR=0、TPR=1；
- 真实模型介于两者之间，越贴近左上角越好。

**AUC**（Area Under the Curve，曲线下面积）的概率解释：随机抽一个正样本和一个负样本，正样本得分高于负样本的概率。AUC 只看**分数的排序**、不依赖某个具体阈值，因此是对「模型区分能力」的整体度量；要落地成决策仍需回到混淆矩阵选阈值。

#### 4.3.2 核心流程

roc_curve 三步法（代码对每一折重复执行）：

```text
第 1 步：fit 后取连续分数
    probas = pipe_lr.fit(...).predict_proba(X_test_fold)
    scores = probas[:, 1]        # 第 1 列 = P(正类=1)（共 2 列，每行和为 1）

第 2 步：roc_curve 生成曲线点
    fpr, tpr, thresholds = roc_curve(y_true, scores, pos_label=1)
    # 内部把所有唯一得分依次当作阈值，逐点统计 (FPR, TPR)，并返回对应阈值

第 3 步：auc + plot
    roc_auc = auc(fpr, tpr)      # 梯形法则算曲线下面积
    plt.plot(fpr, tpr, label=f'ROC fold {i+1} (area = {roc_auc:.2f})')
```

多条折的平均 ROC：各折的 `fpr` 网格不一致，先用 `interp(mean_fpr, fpr, tpr)` 把每折曲线**插值到统一的 101 点 FPR 网格**上再逐点平均；两个端点手工钉死（`mean_tpr[0] = 0.0`、`mean_tpr[-1] = 1.0`），保证曲线从 (0,0) 出发、到 (1,1) 终止。

#### 4.3.3 源码精读

**换一条更朴素的管线，故意留出误差空间。**

[ch06/ch06.py:605-612](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L605-L612) —— ROC 一节不再用调好的 SVM，而是新建 `StandardScaler + PCA(2) + LogisticRegression(C=100.0, solver='lbfgs')` 管线，并且 `X_train2 = X_train[:, [4, 14]]` 只取第 5、15 两个特征列。只用两个特征时逻辑回归有可观的误分率，ROC 曲线才会呈现有信息量的过渡段（若用全部 30 个特征，曲线几乎贴住左上两条边，反而看不出细节）。

**三折循环：fit → predict_proba → roc_curve → interp → auc。**

[ch06/ch06.py:615-635](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L615-L635) —— `cv = list(StratifiedKFold(n_splits=3).split(X_train, y_train))` 把分层三折的索引转成列表；循环内对每折：在训练折上 `fit`、在验证折上 `predict_proba` 取 **`probas[:, 1]`**（第 1 列才是正类概率，与 `pos_label=1` 对应）；[ch06/ch06.py:627-629](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L627-L629) 的 `roc_curve` 返回 `fpr, tpr, thresholds` 三个数组；`interp(mean_fpr, fpr, tpr)` 把该折曲线插值到 `np.linspace(0, 1, 100)` 的统一网格上累加（[ch06/ch06.py:619-631](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L619-L631)，`from numpy import interp` 见 [ch06/ch06.py:30](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L30)）；每折用 `auc(fpr, tpr)` 算面积并画线，图例带上 `(area = %.2f)`。

**三条参考线 + 平均 ROC。**

[ch06/ch06.py:637-652](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L637-L652) —— 依次画三条参照：灰色虚线对角线 `label='Random guessing (area = 0.5)'`；平均 ROC——`mean_tpr /= len(cv)` 求平均、端点钉到 0/1、`mean_auc = auc(mean_fpr, mean_tpr)` 后以黑色虚线画出；以及完美参照折线 `plt.plot([0, 0, 1], [0, 1, 1], ...)`，即**横平竖直贴左上角的「Perfect performance (area = 1.0)」**——这就是实践任务里要标出的「AUC=1 的图例」。最后 [ch06/ch06.py:654-662](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L654-L662) 设轴限、轴名（False/True positive rate）、`legend(loc='lower right')` 并 `plt.show()`。成图即 [ch06/figures/06_10.png](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/figures/06_10.png)。

#### 4.3.4 代码实践

1. **实践目标**：复现三折 ROC 图，读懂每条线的含义。
2. **操作步骤**：完整运行 [ch06/ch06.py:605-662](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L605-L662)；然后单独打印某折的 `thresholds` 数组长度与首尾值。
3. **需要观察的现象**：
   - 三条折的 ROC 各不相同（AUC 不同）——同一模型在不同验证折上的区分能力有波动；
   - 黑色平均 ROC 平滑地落在三条折中间，`Mean ROC (area = ...)` 图例给出平均 AUC；
   - 对角线（随机猜测）在最下方，完美折线紧贴图框左上两边。
   - `thresholds` 从大到小排列，且首端包含一个大于 1 的哨兵值——对应 FPR=TPR=0 的「全预测负类」端点。
4. **预期结果**：三折 AUC 与平均 AUC 均明显大于 0.5、小于 1.0（两特征逻辑回归的实际数值待本地验证）。
5. 若想看「近乎完美」的对比：把 `X_train2 = X_train` 换成全部 30 个特征重跑，曲线会几乎贴住左上角——直观感受特征量对区分能力的影响（示例代码，非书中原句）。

#### 4.3.5 小练习与答案

**练习 1**：为什么取 `probas[:, 1]` 而不是 `probas[:, 0]`？

**答案**：`predict_proba` 返回形状 (n_samples, 2) 的矩阵，第 k 列是 P(类别 k)。`roc_curve(..., pos_label=1)` 要的是「正类 1 的得分」，所以取第 1 列。若取第 0 列（负类概率），分数方向反转，曲线会被压到对角线以下——方向反了，AUC 变成 1−AUC。

**练习 2**：AUC 与 F1 有什么本质区别？

**答案**：F1 基于某个固定阈值给出的硬标签，是「一个工作点」的指标；AUC 遍历所有阈值，是「整条曲线（模型排序能力）」的指标，与阈值选择无关。AUC 高说明分数排得好，但要投产仍需选阈值（回到混淆矩阵/Precision-Recall 权衡）；F1 则直接反映当前决策规则的表现。

**练习 3**：为什么平均 ROC 要用 `interp` 插值，而不能直接把三条折的 `(fpr, tpr)` 点拼起来平均？

**答案**：各折的 `roc_curve` 只在「该折出现过的唯一得分」处产生点，三折的 FPR 取值网格各不相同、点数也不同，无法逐点对齐求平均。`interp` 把每折曲线重采样到同一个 `mean_fpr = linspace(0, 1, 100)` 网格上，才有了逐点平均的共同横坐标。

### 4.4 类别不平衡与指标反转检查：准确率的陷阱

#### 4.4.1 概念说明

真实数据里类别常常不平衡：欺诈交易、疾病筛查、故障检测里正类可能只占百分之几。此时准确率失效——**把所有样本都预测成多数类**就能得到「漂亮」的分数。正确姿势有两条：

1. **换指标**：用显式指定正类的 precision/recall/F1（4.2），或整体看 AUC（4.3）；检查时先问一句「这个指标算的是哪个类？」——这就是**指标反转检查**。
2. **换数据**：重采样让类别均衡。书中演示了对少数类**有放回地上采样（upsampling）**到与多数类同等规模；也可以反向下采样多数类（代价是丢弃信息）。

#### 4.4.2 核心流程

```text
构造人工不平衡数据：357 个类 0 + 40 个类 1（恶性只留 40 个）
→ 傻瓜基线：全预测 0 → accuracy ≈ 89.9%，但正类 F1 = 0   ← 准确率的陷阱
→ resample(少数类, replace=True, n_samples=357) 有放回复制
→ 重新拼接：357 + 357 = 714，两类均衡
→ 同一个傻瓜基线现在只有 50% 准确率 → 准确率恢复「话语权」
```

#### 4.4.3 源码精读

**人工制造不平衡 + 傻瓜基线。**

[ch06/ch06.py:680-687](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L680-L687) —— `np.vstack((X[y == 0], X[y == 1][:40]))` 取全部 357 个类 0 样本、只留前 40 个类 1 样本，拼成 397 个样本的失衡数据集（正类占比 ≈ 10.1%）；`y_pred = np.zeros(y_imb.shape[0])` 是「全预测 0」的傻瓜分类器，`np.mean(y_pred == y_imb) * 100` 算出准确率 ≈ 89.9%（357/397）。**但拿 4.2 的公式检查：TP = 0 ⇒ PRE、REC、F1 对正类全为 0**——这就是「F1 = 0 而 accuracy ≈ 90%」的名场面，也是练习里指标反转检查的标准案例：默认 `pos_label=1`（少数类）恰好暴露了问题；若把正类错当多数类看，指标又会「好看」起来，陷阱被掩盖。

**有放回上采样。**

[ch06/ch06.py:693-701](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L693-L701) —— `resample(X_imb[y_imb == 1], y_imb[y_imb == 1], replace=True, n_samples=X_imb[y_imb == 0].shape[0], random_state=123)`：只对少数类样本操作，`replace=True` 允许重复抽取，把 40 个样本有放回地扩到 357 个（`n_samples` 对齐多数类规模），打印前后数量对比。`from sklearn.utils import resample` 见 [ch06/ch06.py:31](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L31)。

**均衡之后，傻瓜基线现形。**

[ch06/ch06.py:706-713](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L706-L713) —— `np.vstack((X[y == 0], X_upsampled))` 重新拼成 714 个样本（注意多数类用的是原始 `X[y == 0]`），同样的全预测 0 基线如今准确率只剩 50%——数据均衡后，准确率重新变成有意义的指标，任何模型想超过 50% 就必须真的学会区分两类。

#### 4.4.4 代码实践

1. **实践目标**：亲眼看到「accuracy 89.9% 但 F1 = 0」，并理解上采样的意义。
2. **操作步骤**：
   - 运行 [ch06/ch06.py:680-687](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L680-L687) 构造 `y_imb` 与全零 `y_pred`；
   - 补一行（示例代码）：`f1_score(y_imb, y_pred, pos_label=1)` 与 `precision_score(y_imb, y_pred, pos_label=1)`；
   - 再运行 [ch06/ch06.py:693-707](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L693-L707) 完成上采样并重拼 `y_bal`；
   - 最后看 [ch06/ch06.py:712-713](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L712-L713) 的傻瓜准确率。
3. **需要观察的现象**：失衡版 accuracy ≈ 89.9% 而 precision/recall/F1 全为 0（TP=0，0/0 在 sklearn 中记 0 并伴随 UndefinedMetricWarning）；均衡版同一基线 accuracy = 50%。
4. **预期结果**：打印 `Number of class 1 examples before: 40`、`after: 357`；两次傻瓜准确率分别约 89.9 与 50.0（397/714 的精确小数为待本地验证）。
5. 思考：上采样只是让「指标不撒谎」，真正训练时还应配合交叉验证（在折内重采样，避免同一份复制的样本同时出现在训练折与验证折造成泄漏）。

#### 4.4.5 小练习与答案

**练习 1**：为什么不直接下采样多数类到 40 个？

**答案**：可以，但会丢掉 317 个多数类样本的信息，而数据通常宝贵（本例每个良性样本的测量都花了成本）。上采样保留全部多数类信息，代价只是少数类样本被复制多份（可能加重过拟合风险）。两者是同一权衡的两端：下采样丢信息、上采样重复信息，实践中也常结合使用。

**练习 2**：`resample(..., random_state=123)` 这个参数起什么作用？

**答案**：钉住有放回抽样的随机种子，让「40 → 357」的复制结果可复现。没有它，每次运行抽到的样本组合不同，实验无法精确重放。这与 `train_test_split(random_state=1)` 的动机一致——随机性只出现在我们指定的地方。

**练习 3**：上采样为什么必须 `replace=True`？

**答案**：少数类只有 40 个互不相同的样本，要抽 357 个又不放回，最多只能抽 40 个，直接报错（无法取到 357 个不重复样本）。有放回抽样才允许同一样本被多次选中，从而把少数类「膨胀」到目标规模。

## 5. 综合实践

**任务：为乳腺癌数据集做一次「调参 → 全面体检」的完整评估。**

前提：沿用 u6-l1/u6-l2 的数据划分（[ch06/ch06.py:139-142](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L139-L142)）与 SVM 管线（[ch06/ch06.py:334-343](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L334-L343)），测试集只在最后评估时用一次。步骤：

1. **网格搜索出最优管线**：运行 [ch06/ch06.py:345-363](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L345-L363) 的 `GridSearchCV`（`refit=True`），拿到已在训练集上重训好的 `gs.best_estimator_`。
2. **混淆矩阵与热图**：按 4.1 打印矩阵并绘制热图，标出 TP/FP/FN/TN 四个格子（预期 71/1/2/40）。
3. **三种平均的 F1**：按 4.2 打印 `average='micro'/'macro'/'weighted'` 三个 F1，验证 micro ≈ accuracy（≈0.974）、macro 略低、weighted 居中。
4. **指标反转检查（坑与修正）**：再打印默认 F1（正类=1）与 `pos_label=0` 的 F1，两者应不同（≈0.964 vs ≈0.979）；随后**故意**用 4.4 的全零傻瓜预测验证极端情形——accuracy 可以很高而 F1 = 0，此时检查你的 `pos_label` 是否指向少数类；修正方式即 [ch06/ch06.py:578](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L578) 的 `make_scorer(f1_score, pos_label=...)` 显式声明正类。
5. **ROC 与 AUC**：按 4.3 画出三折 ROC 图，确认图上同时出现：三条折线图例（各自 area）、Random guessing（area = 0.5）、Mean ROC、以及 **Perfect performance (area = 1.0)** 的 AUC=1 图例。

预期产出：一张热图、五个以上的指标数字、一张 ROC 图，以及一句话结论——「模型对少数类（恶性）的查全率 recall ≈ 0.95，漏诊 2 例；若业务要求零漏诊，应把阈值向提高 recall 的方向移动并接受更多 FP」。精确数字待本地验证。

## 6. 本讲小结

- 混淆矩阵（[ch06/ch06.py:498-518](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L498-L518)）把准确率拆成 TP/FP/TN/FN 四格；`labels=[1, 0]` 可以翻转行列顺序、把真阳性放到左上角。
- precision 报警准不准、recall 漏没漏、F1 调和平均惩罚偏科、MCC 综合四格（[ch06/ch06.py:562-572](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L562-L572)）；多分类按 micro（=准确率）/macro（少数类同权）/weighted（按样本量加权）平均。
- 二分类指标默认 `pos_label=1` 且静默生效——必须确认它指向你关心的（往往是少数）类；`make_scorer(f1_score, pos_label=...)`（[ch06/ch06.py:578](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L578)）让调参目标与评估口径一致。
- ROC 三步法 `predict_proba → roc_curve → auc`（[ch06/ch06.py:623-635](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L623-L635)）：扫过所有阈值画 (FPR, TPR) 曲线，AUC 是「正样本得分排在负样本前」的概率；平均 ROC 用 `interp` 重采样到统一网格再逐点平均。
- 类别不平衡时准确率会撒谎：全预测多数类的傻瓜模型在 357:40 的数据上 accuracy ≈ 89.9% 而 F1 = 0（[ch06/ch06.py:680-687](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L680-L687)）；`resample(replace=True)` 上采样少数类到与多数类同规模后，傻瓜基线跌回 50%。

## 7. 下一步学习建议

本讲完成了第 6 章「模型评估」的最后一块拼图。下一讲进入 **u7-l1 多数投票集成从零实现**：书中会用 `ensemble_error` 概率推导「什么条件下集成优于单分类器」，并在对比基线模型与集成时**直接复用本讲的 10 折交叉验证与 AUC/ROC 指标**——建议带着一个问题去读：当多个基分类器投票时，它们的 precision/recall/F1 会如何互相补偿？此外可以继续阅读 scikit-learn 文档中 `classification_report`（一次性输出每类的 precision/recall/F1 表格）与 `precision_recall_curve`（不平衡数据上常比 ROC 更敏感的替代曲线），把它们作为本讲指标体系的自然延伸，在乳腺癌数据集上自行练习。
