# 文本指标：APE、δ@1.25、LLM judge 与 GM-δ

## 1. 本讲目标

上一讲（u6-l2）我们把 `eval_ground3d.sh` 跑通并理解了「数据集 × QA 层级 × 子任务」三层循环与 LoRA rank 探测；更早（u6-l1）我们掌握了分割侧的 mIoU 怎么算。但 Ground3D-LMM 的输出不只是分割掩码——它还会**用自然语言回答问题**（"这把椅子离相机大约 2.3 米"）。自然语言答案无法用 IoU 衡量，本项目为此提供了一套独立的文本评测工具 `tools/evaluate_text_metrics.py`。

学完本讲，你应该能够：

1. **推导 APE 与 δ@1.25 的计算规则**：理解 LLM 数字抽取 → 数值配对 → 逐对误差 → 逐 QA 平均 → 逐任务宏观平均的完整链路，并说清楚「预测缺数被罚 0.0」在公式里意味着什么。
2. **说明 GM-δ 如何同时要求 mask IoU 与数值精度**：一条预测要同时「接地接对了」（IoU > 0.3）且「数说得准」（δ ≤ 1.25）才算成功。
3. **解释 judge 阶段的提示构造与打分含义**：看懂 VL judge（Qwen3-VL-4B-Instruct）收到的完整 prompt，理解幻觉（hallucination）与完整性（completeness）两个 1-10 分的含义与聚合方式。
4. 掌握「任务感知的递归展平」——如何把五花八门的预测 JSON 目录树变成统一的带任务标签的样本列表。

本讲所有指标**都不需要 GPU 也能学**：APE/δ 的数学部分有内置单元测试（`--selftest`），展平与配对逻辑是纯 Python 函数，可直接导入验证。

## 2. 前置知识

- **APE（Absolute Percentage Error，绝对百分比误差）**：衡量预测值 \(\hat{s}\) 偏离真值 \(s\) 的比例：

  \[ \mathrm{APE} = \frac{|s - \hat{s}|}{s} \times 100\% \]

  值越小越好，0 表示完全一致。

- **δ 阈值指标（delta@1.25）**：来自深度估计领域的经典做法。定义比值

  \[ \delta = \max\left(\frac{\hat{s}}{s},\ \frac{s}{\hat{s}}\right) \ge 1 \]

  它对「预测偏大」和「预测偏小」对称。当 \(\delta \le 1.25\)（即预测与真值相差不超过 25%）记一次成功，报告成功率 δ@1.25。注意 \(\hat{s}=0\) 时比值定义为无穷大——这是「预测缺失数字」受到的最重惩罚。

- **LLM-as-judge（用大模型当裁判）**：让一个 LLM 读「问题 + 标准答案 + 模型答案」，按维度打 1-10 分。本项目用它评幻觉与完整性。判分用**贪心解码**（`do_sample=False`）：每步取概率最高的 token，保证同一输入可复现——评测脚本必须确定性，否则分数无法对比。

- **宏观平均（macro-average）与池化平均**：先在每条 QA 内部对多个数值对求均值（QA 级分数），再对同任务的 QA 分数求均值（任务级 = 对 QA 等权的宏观平均）；overall 则把所有任务的 QA 分数**倒进一个池子**再平均（对 QA 等权、但对任务按样本量加权）。

- **mask dump（掩码落盘）**：u3-l1 提过 `MASK_DUMP_DIR`——推理时把每个场景逐 QA 的点级预测/GT 掩码存成 `{scene}_qa_pred_masks.npy` 与 `{scene}_qa_gt_masks.npy`。GM-δ 阶段靠它把「文本答案」和「分割掩码」两条输出通道重新 join 到一起。

- **贪心解码 / 重复惩罚**：贪心见上；重复惩罚（repetition penalty，如 1.05）会把已出现 token 的概率除以该系数，抑制翻来覆去说同一句话。抽取模型用了它，judge 模型没用。

- **目录镜像（mirror）**：u6-l2 讲过 `eval_ground3d.sh --mask-dump` 会让掩码目录树**逐层复制**预测目录树的结构。GM-δ 的 join 就靠这份镜像 + 同名约定。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲涉及范围 |
|---|---|---|
| [tools/evaluate_text_metrics.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/evaluate_text_metrics.py) | 本讲主角：ape / judge / gmdelta 三个文本评测阶段 | 全文件（593 行） |
| [uniseg3d/uniseg3d.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py) | `Grounded_LMM_main.predict` 中的 mask dump 代码，产出 GM-δ 需要的 npy | L1310-L1326 |
| [eval_ground3d.sh](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/eval_ground3d.sh) | 掩码目录镜像的实现（`MASK_DUMP_DIR` 环境变量拼接） | L136-L137 |
| [README.md](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md) | Step 2/3/3b 三段官方用法与指标出处表 | L271-L325 |

脚本总体分五段：展平（L60-113）、本地 LLM 封装（L116-158）、ape 阶段（L161-318）、judge 阶段（L321-412）、gmdelta 阶段（L415-507），外加单元测试与 CLI（L510-588）。

## 4. 核心概念与源码讲解

### 4.1 任务感知的递归展平：flatten_with_task 与 gather_samples

#### 4.1.1 概念说明

u6-l2 的评估启动器把预测按「数据集/层级/子任务」归档成目录树，u4-l5 又告诉我们预测 JSON 有单轮（三级键嵌套）和多轮（`multi_turn_qa_data` 列表）两种结构。三个评测阶段需要的却是一个统一形态：**一个扁平列表，每个元素是 `{question, gt_answer, pred_answer, _task, ...}`**，其中 `_task` 标明该样本属于哪个子任务——因为 ape 只跑 4 个数值任务、judge 跑全部任务、gmdelta 按任务名过滤目录。

「任务感知」体现在 `_task` 的三级回退取值：JSON 内部标注 > 包裹它的字典键 > 文件路径中的目录名。

#### 4.1.2 核心流程

```text
gather_samples(input_path)
  ├─ input 是文件？→ 只用它；是目录？→ rglob("*.json") 递归收集
  ├─ 逐文件 json.load（失败仅告警跳过，不中断整个评测）
  ├─ _task_from_path：从路径 parts 倒序找第一个已知任务名
  └─ flatten_with_task(data)：递归遍历
       ├─ dict 且同时含 pred_answer 与 gt_answer → 识别为一个样本
       │    _task = meta_info.group（若有）或 包裹键 key
       ├─ 其他 dict → 对每个 (k, v) 递归，key 换成 k
       └─ list → 逐元素递归（key 原样下传，保持外层任务名）
  最后：样本的 _task 缺失或不在已知任务集 → 用路径任务名兜底
```

关键点：**递归时 key 会被最近的字典键覆盖**，所以单轮结构 `{part_qa: {场景: {distance_estimation: [样本...]}}}` 里的样本最终拿到 `distance_estimation`（最近的包裹键），而不是 `part_qa`；多轮结构里列表元素拿到的键是 `multi_turn_qa_data`。

#### 4.1.3 源码精读

[tools/evaluate_text_metrics.py:L61-L80](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/evaluate_text_metrics.py#L61-L80) 是递归展平本体：判定「同时含 `pred_answer` 和 `gt_answer`」即为样本，`_task` 优先取 `meta_info.group`（L70），否则用最近的包裹键。

[tools/evaluate_text_metrics.py:L83-L94](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/evaluate_text_metrics.py#L83-L94) 定义已知任务集合与路径探测：`_KNOWN_TASKS` = 4 个数值任务并上其余 5 个任务名（含 `multi_turn_qa_data`）；`_task_from_path` 倒序扫路径段，返回第一个命中者。

[tools/evaluate_text_metrics.py:L97-L113](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/evaluate_text_metrics.py#L97-L113) 汇总入口：`rglob` 递归找 JSON、坏文件告警跳过（L103-107）、以及 `_task` 的路径兜底逻辑（L110-111）——只有当 JSON 内部标签**缺失或不在已知集合**时才用路径名替换，这就是「三级回退」的最后一环。

另外注意 [tools/evaluate_text_metrics.py:L55-L57](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/evaluate_text_metrics.py#L55-L57) 定义的 `clean_text`（用正则剥掉 `<SEG>`、`<p>` 等特殊标记）：**当前版本中它定义了但没有任何调用点**（可用 grep 验证），judge 收到的是未清洗的原始答案。这是阅读时的一个「坑」，4.3 节会再提。

#### 4.1.4 代码实践

**实践目标**：不跑任何模型，亲手构造一个迷你预测目录树，验证展平与 `_task` 标注逻辑。

1. 操作步骤（以下为**示例代码**，请在仓库外的临时目录保存运行，不要写入仓库）：

   ```python
   # mini_flatten_test.py — 示例代码
   import json, tempfile, sys
   from pathlib import Path
   sys.path.insert(0, "<仓库路径>")           # 使 tools 可导入
   from tools.evaluate_text_metrics import gather_samples

   root = Path(tempfile.mkdtemp())
   # 模拟 u6-l2 产物结构：pred_qa_data_val/scannet/part_qa_data/.../distance_estimation/scene0000_00.json
   d = root / "pred_qa_data_val/scannet/part_qa_data/split/distance_estimation"
   d.mkdir(parents=True)
   (d / "scene0000_00.json").write_text(json.dumps({
       "part_qa": {"scene0000_00_frame_0": {"distance_estimation": [
           {"question": "How far is the chair from the camera?",
            "gt_answer": "About <p>the chair</p> is 0.9m away. <SEG>",
            "pred_answer": "The chair is about 1.2 meters away."}]}}, }))
   for s in gather_samples(root):
       print(s["_task"], "|", s["question"][:30], "| pred:", s["pred_answer"][:30])
   ```

2. 需要观察的现象：`_task` 是 `distance_estimation`（最近的包裹键），而不是 `part_qa`；`gt_answer` 中的 `<p>`、`<SEG>` 原样保留。
3. 预期结果：打印一行样本，任务标签正确。
4. 本实践纯 CPU、无网络、无模型依赖；若导入报错请确认 `sys.path` 指向仓库根。完整跑通属于「待本地验证」（逻辑本身由源码保证）。

#### 4.1.5 小练习与答案

**练习 1**：多轮预测 JSON（`{"multi_turn_qa_data": [[{...}, {...}]]}`）展平后，样本的 `_task` 是什么？
**答案**：`multi_turn_qa_data`——列表递归时包裹键原样下传，列表内元素最近的字典键就是它。

**练习 2**：若某个预测 JSON 解析失败（比如被截断），整个评测会崩溃吗？
**答案**：不会。[L103-L107](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/evaluate_text_metrics.py#L103-L107) 用 try/except 捕获后仅打印 `[WARN] skipping` 并继续处理其余文件——评测工具对脏数据要有韧性。

**练习 3**：为什么 `_task` 要设计「JSON 内部标签 → 包裹键 → 路径名」三级回退？
**答案**：不同来源的预测 JSON 嵌套层级不统一（有的样本带 `meta_info.group`，有的没有；有的目录名即任务名）。三级回退保证任何一种产物形态都能拿到可靠任务标签，后续按任务过滤（ape 只留数值任务）才有依据。

### 4.2 ape 阶段：LLM 数字抽取与 APE/δ@1.25

#### 4.2.1 概念说明

自然语言回答里的数字五花八门："大约 2.3 米"、"长 1.78m 宽 0.78m 厚 0.53m"。要算数值误差，第一步得把数字**结构化抽取**出来。本项目的做法是让一个文本 LLM（默认 `Qwen/Qwen3-4B-Instruct-2507`）读「任务类型 + 问题 + GT 答案 + 预测答案」，输出规定形状的 JSON；第二步把 GT 与预测的数字按任务规则配对；第三步逐对计算 APE 与 δ。

只有 4 个「数值任务」参与（[tools/evaluate_text_metrics.py:L44-L50](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/evaluate_text_metrics.py#L44-L50)）：`distance_estimation`（抽一个 `distance_m`）、`grounded_dimension_reasoning` 与 `scale_estimation`（抽 `length_m`/`width_m`/`thickness_m`/`distance_from_camera_m` 中适用者）、`scale_comparison_size`（两个物体各抽 `[长, 宽, 厚]`）。

**罚 0 规则**是理解本阶段的关键：GT 有某数字而预测没有 → 预测侧补 `0.0`。代入公式，APE = 100%、δ = ∞、成功 = 0——漏答数字不会被跳过，而是被记一次最差成绩。

#### 4.2.2 核心流程

```text
stage_ape(samples, llm, tau)
  ├─ 过滤：只留 _task ∈ NUMERIC_TASKS 的样本
  ├─ 逐样本：
  │    ├─ LLM 抽取 → parse_extraction 得 (extracted_gt, extracted_pred)
  │    │    （JSON 解析失败：distance 任务有正则兜底，其余记 extraction_failure 并跳过）
  │    ├─ fill_missing_with_zero：GT 有而 pred 缺的键 → 0.0（列表 → [0,0,0]）
  │    ├─ get_pairs_for_task：按任务规则把两侧数字配成 [(g, p), ...]
  │    └─ ape_delta_from_pairs → {ape_percent, delta_ratio, delta_success, n_pairs}
  ├─ 逐任务：对 QA 级结果求均值（宏观平均）
  └─ overall：把所有任务的 QA 级结果池化后求均值
```

数学规则（[ape_delta_from_pairs](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/evaluate_text_metrics.py#L266-L280)）：

- 逐对：若 \(s = 0\) 该对**跳过**（GT 本身为 0 无法作分母）；否则

  \[ \mathrm{APE}_i = \left|\frac{s_i - \hat{s}_i}{s_i}\right| \times 100, \qquad \delta_i = \begin{cases} \infty & \hat{s}_i = 0 \\ \max(\hat{s}_i / s_i,\ s_i / \hat{s}_i) & \text{否则} \end{cases} \]

  成功指示：\(\mathbb{1}[\delta_i \le \tau]\)，默认 \(\tau = 1.25\)。

- QA 内：对 pairs 求均值得该 QA 的 `ape_percent`、`delta_success`。`delta_ratio` 只要有一对是 ∞，均值就报 ∞（不做截断平均）。

#### 4.2.3 源码精读

**LLM 封装**：[tools/evaluate_text_metrics.py:L117-L158](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/evaluate_text_metrics.py#L117-L158) 的 `LocalLLM` 用 transformers 直接加载（无需 vLLM/服务）：`--gpus` 写入 `CUDA_VISIBLE_DEVICES`，`device_map='auto'` 把模型分片到多卡；`chat()` 组 system+user 消息、`apply_chat_template`、贪心 `generate`（causal 后端带 `repetition_penalty=1.05`），只解码新增 token。

**抽取提示词**：[tools/evaluate_text_metrics.py:L162-L187](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/evaluate_text_metrics.py#L162-L187) 的 `EXTRACTION_SYSTEM_PROMPT` 按三类任务给出精确的输出 JSON 形状，并在 RULES 里写死罚 0 约定（"If gt has a value and pred does not, set the pred value to 0.0"）——**提示词层面的约定稍后会在代码层面二次强制**（`fill_missing_with_zero`），双保险。user 侧拼接见 [L190-L193](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/evaluate_text_metrics.py#L190-L193)。

**响应解析**：[tools/evaluate_text_metrics.py:L196-L214](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/evaluate_text_metrics.py#L196-L214) 的 `parse_extraction` 先剥 markdown 代码围栏，再尝试「整串解析」与「正则截取第一个 `{...}`」两条路，要求同时含 `extracted_gt`/`extracted_pred` 两键；全失败时**仅 distance 任务**有正则兜底（`(\d+\.?\d*)\s*(?:m|meters?|metres?)` 抓两个带单位的数字，凑不出第二个则 pred 记 0.0）。

**罚 0 强制**：[tools/evaluate_text_metrics.py:L217-L222](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/evaluate_text_metrics.py#L217-L222) 的 `fill_missing_with_zero` 以 GT 的键为模板，pred 缺的键补 0.0；GT 值是列表时补 `[0.0, 0.0, 0.0]`（对应 `scale_comparison_size` 的两物体三维）。

**配对规则**：[tools/evaluate_text_metrics.py:L233-L263](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/evaluate_text_metrics.py#L233-L263) 的 `get_pairs_for_task` 按** GT 侧的键**驱动配对（GT 没提到的维度不配对），pred 侧取不到按 0.0；`scale_comparison_size` 把两个物体 × 三维展开成至多 6 对。

**指标计算与汇总**：[tools/evaluate_text_metrics.py:L266-L280](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/evaluate_text_metrics.py#L266-L280) 实现上面的公式；[L284-L318](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/evaluate_text_metrics.py#L284-L318) 的 `stage_ape` 完成过滤、逐 QA 计算与两级汇总，并记录 `extraction_failures`（抽取彻底失败的样本数，不计入分母）。

官方用法在 [README.md:L279-L288](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L279-L288)（Step 2）。

#### 4.2.4 代码实践

**实践目标**：零 GPU 验证 APE/δ 数学实现与「罚 0」行为。

1. 操作步骤：
   - 第一步，跑内置单元测试（不需要 LLM、不需要数据、不需要 GPU）：

     ```bash
     python tools/evaluate_text_metrics.py --selftest
     ```

     它运行 [L511-L531](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/evaluate_text_metrics.py#L511-L531) 的 4 组参考夹具（T1-T4），对每组校验 `ape_percent`、`delta_ratio`、`delta_success` 与期望值的偏差小于阈值。

   - 第二步（**示例代码**，在临时目录运行），手动复算 T2 并试验罚 0：

     ```python
     # ape_hand_check.py — 示例代码
     import sys, math
     sys.path.insert(0, "<仓库路径>")
     from tools.evaluate_text_metrics import (
         get_pairs_for_task, ape_delta_from_pairs, fill_missing_with_zero)

     eg = {"length_m": 1.78, "width_m": 0.78, "thickness_m": 0.53}
     ep = {"length_m": 2.1,  "width_m": 0.9,  "thickness_m": 0.7}
     print(ape_delta_from_pairs(get_pairs_for_task("scale_estimation", eg, ep)))
     # 罚 0：预测完全没给数字
     print(ape_delta_from_pairs(get_pairs_for_task(
         "scale_estimation", eg, fill_missing_with_zero(eg, {}))))
     ```

2. 需要观察的现象：第一组输出 `ape_percent=21.8125, delta_ratio=1.2181, delta_success=0.6667`；第二组 `ape_percent=100.0, delta_ratio=inf, delta_success=0.0`。
3. 预期结果推导（第一组）：
   - 长：\(|1.78-2.1|/1.78 = 17.98\%\)，\(\delta = 2.1/1.78 = 1.180\)，成功；
   - 宽：\(0.12/0.78 = 15.38\%\)，\(\delta = 1.154\)，成功；
   - 厚：\(0.17/0.53 = 32.08\%\)，\(\delta = 1.321 > 1.25\)，失败；
   - 均值：APE \(= (17.98+15.38+32.08)/3 = 21.81\%\)，成功率 \(= 2/3 = 0.6667\)。
4. `--selftest` 可直接运行；第二步脚本属示例代码，运行结果**待本地验证**（数值应与上面推导一致）。

#### 4.2.5 小练习与答案

**练习 1**：GT 是 0.9 米、预测是 3.5 米（即 T1 夹具），APE 和 δ 各是多少？
**答案**：APE \(= |0.9-3.5|/0.9 = 289.32\%\)（可以超过 100%！）；\(\delta = 3.5/0.9 = 3.89 > 1.25\)，成功 0。这正是 [L513](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/evaluate_text_metrics.py#L513) 的期望值。

**练习 2**：为什么 GT==0 的数值对要跳过而不是也罚 0？
**答案**：GT 为 0 时 APE 的分母为 0、δ 的比值也无定义（任何非零预测都给出 \(\delta=\infty\)）。这类对不携带有效监督信息，跳过是唯一合理选择；「罚 0」只用于 **GT 有值而预测缺失**的情形。

**练习 3**：`stage_ape` 的 overall 是「各任务均值的均值」吗？
**答案**：不是。[L304-L317](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/evaluate_text_metrics.py#L304-L317) 先 `all_qa += lst` 把所有任务的 QA 级结果**池化**再求均值——样本量大的任务权重更大；逐任务的 `per_task` 才是对该任务内 QA 等权的宏观平均。

### 4.3 judge 阶段：VL judge 打分幻觉与完整性

#### 4.3.1 概念说明

ape 只覆盖 4 个数值任务，但模型在**全部任务**上的回答质量（说不说得对、全不全、有没有编造）也需要量化。本项目采用 LLM-as-judge：用**Qwen3-VL-4B-Instruct**（与被评模型同家族的视觉语言模型，即论文 Table 4 的 judge）对每条预测打 1-10 分，最终只聚合其中两个维度：

- **Hallucination（幻觉，越高越好）**：10 = 每句话都有 GT 支撑、没有编造；1 = 大部分内容是编的。
- **Completeness（完整性，越高越好）**：GT 里的信息点预测覆盖得有多全。

提示词还要求 judge 顺手评 `accuracy`、`reasoning_quality`，并对双侧都含数字的情况给 `metric_accuracy`（按平均百分比差异分档：<5% → 10 分，5-15% → 8-9 分，15-40% → 5-7 分，>40% → 1-4 分）——但 [stage_judge](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/evaluate_text_metrics.py#L394-L412) **只读取并聚合 `hallucination` 与 `completeness`**，其余字段被丢弃。数值精度已由 ape 阶段用硬公式覆盖，这里不必重复。

#### 4.3.2 核心流程

```text
stage_judge(samples, vl_llm)
  ├─ 对每条样本（全部任务）：
  │    ├─ JUDGE_SYSTEM + build_judge_user(question, gt, pred) → VL 模型
  │    │    （贪心解码，max_new_tokens=512，无重复惩罚）
  │    ├─ _parse_judge_json：剥围栏 / 截取 {..}，json.loads
  │    └─ hallucination 与 completeness 均为数值 → 累加并 n+=1
  │       （JSON 解析失败或字段非数值 → 该样本静默跳过，不计分母）
  └─ 汇报 hallucination、completeness（两位均值）与 n_judged
```

#### 4.3.3 源码精读

[tools/evaluate_text_metrics.py:L324-L329](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/evaluate_text_metrics.py#L324-L329) 的 `JUDGE_SYSTEM` 限定 judge 角色、1-10 分制、「只返回 JSON」。

[tools/evaluate_text_metrics.py:L332-L378](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/evaluate_text_metrics.py#L332-L378) 的 `build_judge_user` 是本阶段的核心提示：Question / Ground truth / Model predicted 三段原文，随后逐维度给出评分口径（如 hallucination："10 = no hallucination, strictly supported by GT"），再附 `metric_accuracy` 的数值分档细则与目标 JSON 骨架（双花括号是 f-string 转义，输出单花括号）。值得注意：**输入是原始答案，未经过 `clean_text` 清洗**，GT 里的 `<p>...</p>`、`<SEG>` 等接地标记会原样进入 judge 的视野（[L55-L57](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/evaluate_text_metrics.py#L55-L57) 的 `clean_text` 目前无调用点）。

[tools/evaluate_text_metrics.py:L381-L391](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/evaluate_text_metrics.py#L381-L391) 的 `_parse_judge_json` 与 ape 阶段的解析套路一致（剥围栏 → 整串或首个 `{...}` 尝试 `json.loads`），但不校验具体字段。

[tools/evaluate_text_metrics.py:L394-L412](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/evaluate_text_metrics.py#L394-L412) 的 `stage_judge` 聚合：`isinstance(hv, (int, float))` 双字段校验通过才计入，最后报告两个均值与有效判分数 `n_judged`；一条都没判成时返回 `{"n_judged": 0}` 而不是除零崩溃。

VL 后端的推理细节在 [L143-L151](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/evaluate_text_metrics.py#L143-L151)：Qwen3-VL 的消息内容是 `[{"type": "text", ...}]` 列表形态，`AutoProcessor` 统一编码，切片 `out[0][inputs["input_ids"].shape[1]:]` 只解码新增部分。官方用法见 [README.md:L290-L298](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L290-L298)（Step 3）。

#### 4.3.4 代码实践

**实践目标**：不加载模型，检查 judge 提示的完整构造与 JSON 解析的容错。

1. 操作步骤（**示例代码**，临时目录运行）：

   ```python
   # judge_prompt_test.py — 示例代码
   import sys
   sys.path.insert(0, "<仓库路径>")
   from tools.evaluate_text_metrics import build_judge_user, _parse_judge_json

   print(build_judge_user(
       "How far is the chair from the camera?",
       "About <p>the chair</p> is 0.9m away. <SEG>",
       "The chair is about 1.2 meters away."))
   # 容错测试：带 markdown 围栏 + 前后噪声的 judge 回复
   print(_parse_judge_json('好的，评分如下：\n```json\n{"hallucination": 9, '
         '"completeness": 8, "metric_accuracy": null}\n```\n以上。'))
   ```

2. 需要观察的现象：
   - 打印的 prompt 中 GT 答案里保留了 `<p>`/`<SEG>` 标记（证实 clean_text 未接线）；
   - `metric_accuracy` 的分档说明出现在 prompt 尾部；
   - `_parse_judge_json` 对围栏 + 中文噪声包裹的回复仍解析出字典。
3. 预期结果：第二个 print 输出含 `hallucination: 9`、`completeness: 8` 的 dict。
4. 本实践纯 CPU 无模型；实际用 VL judge 打分需下载 Qwen3-VL-4B-Instruct，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 judge 用 VL 模型（Qwen3-VL）而不是更便宜的纯文本模型？
**答案**：这是论文 Table 4 的设定（[L321-L323](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/evaluate_text_metrics.py#L321-L323) 注明 "The Table-4 judge is Qwen3-VL-4B-Instruct"）。虽然当前 prompt 只发文本，但与被评模型同家族的 judge 对这类回答的分布更熟悉，保证复现论文口径；代码上体现为 `backend='vl'` 走 `Qwen3VLForConditionalGeneration` 加载路径。

**练习 2**：judge 回复的 JSON 里 `hallucination` 写成了字符串 `"9"`，这条样本会计入均值吗？
**答案**：不会。[L406](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/evaluate_text_metrics.py#L406) 的 `isinstance(hv, (int, float))` 校验会跳过它——`n` 不加，该样本既不进分子也不进分母。

**练习 3**：`metric_accuracy` 已经让 judge 评了数值精度，为什么最终报告不聚合它？
**答案**：数值精度由 ape 阶段的**硬公式**（APE/δ，可复现、有单元测试）覆盖；LLM 对数字差异的目测分档噪声大，只适合做辅助解释字段。工程原则：能量化的指标不要交给 LLM 主观打分。

### 4.4 gmdelta 阶段：mask IoU 与 δ 联合判定

#### 4.4.1 概念说明

Ground3D-LMM 的特色是「接地 + 测量」一体：回答"那扇门高约 2.1 米"的同时用 `<SEG>` 指出**是哪扇门**。ape 只查数、mIoU 只查接地，GM-δ（Grounded-Measurement delta，补充材料指标）把两者合取：

\[ \text{GM-δ 成功} \iff \mathrm{IoU}(\text{点级掩码}) > 0.3 \ \wedge\ \delta \le 1.25 \ ]

即**接地接对了 AND 数说得准**才算成功。它回答的问题非常实际：模型会不会"指对了物体但报错尺寸"或"数对了但指错了对象"——两种偏科都拿不到分。

数据来源是两条通道的 join：文本侧是预测 JSON（`--input`），掩码侧是 u6-l2 用 `--mask-dump` 落盘的 npy 目录树（`--mask_dump_dir`）。**join 键是「目录镜像 + 同名 + 按位对齐」三层字符串/位置契约**——这是本模块最需要理解的工程设计。

#### 4.4.2 核心流程

```text
stage_gmdelta(input_path, mask_dump_dir, llm, tasks, iou_thr=0.3, tau=1.25)
  ├─ 递归收集预测 JSON，按「父目录」分组
  ├─ 只处理父目录名 ∈ tasks 的组（默认 4 个数值任务，跨数据集自动发现）
  ├─ 掩码目录 = mask_dump_dir / (预测目录相对于 input 的路径)   ← 镜像
  ├─ 数据集名 = 路径中的 scannet / scannetpp 段（都没有则记 "data"）
  ├─ 逐场景文件（scene.json ↔ scene_qa_pred_masks.npy / scene_qa_gt_masks.npy）：
  │    ├─ 任一 npy 缺失 → 跳过该场景
  │    ├─ 展平 JSON，按 _task 过滤（等于本任务或 None）
  │    └─ 逐 QA（按位对齐第 i 个样本 ↔ 第 i 条掩码）：
  │         ├─ iou  = _mask_iou(gt_masks[i], pred_masks[i])   None 则跳过
  │         ├─ LLM 抽取 → δ 指标；delta_ok = (delta_success == 1.0)
  │         │    （该 QA 的**所有**数值对都 δ ≤ τ 才算过）
  │         ├─ iou_ok  = iou > 0.3
  │         └─ succ = iou_ok AND delta_ok   →  累计 n / iou_ok / delta_ok / succ
  └─ 输出 per (dataset, task) 的 GM-δ 成功率、IoU 过线率、δ 过线率 + overall
```

掩码侧的来源：[uniseg3d/uniseg3d.py:L1310-L1326](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1310-L1326) 在 `Grounded_LMM_main.predict` 末尾，读环境变量 `MASK_DUMP_DIR`（或 `decoder.save_viz_dir`），把每个场景的 `qa_pred_masks`/`qa_gt_masks` 按场景名存成两个 npy（`dtype=object` 的逐 QA 列表，每个元素是 `[n_obj, N]` 布尔点级掩码）。而 [eval_ground3d.sh:L136-L137](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/eval_ground3d.sh#L136-L137) 在启动 Step-1 时设置 `MASK_DUMP_DIR=$MASK_DUMP/${save_dir#"$OUT_ROOT/"}`，使掩码树逐层镜像预测树——于是 `mdir = mask_dump_dir / rel` 就能直接命中。

`_mask_iou` 的口径与 u6-l1 的分割指标一致：GT 与预测掩码行数取 `min` 截断，**GT 行和为 0 的行（无接地/NaN 哨兵）剔除**，逐实例 IoU 后对有效行求均值。

#### 4.4.3 源码精读

[tools/evaluate_text_metrics.py:L416-L436](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/evaluate_text_metrics.py#L416-L436) 的 `_mask_iou`：`to2d` 把列表/一维数组统一成 `[n_obj, N]` 二维布尔阵；`k = min(行数)` 对齐；`valid = g.sum(-1) > 0` 复现「无效 GT 行不计数」；IoU 分母加 `1e-8` 防零。

[tools/evaluate_text_metrics.py:L439-L507](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/evaluate_text_metrics.py#L439-L507) 的 `stage_gmdelta`：

- [L449-L454](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/evaluate_text_metrics.py#L449-L454) 收集预测文件并按父目录分组；
- [L459-L466](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/evaluate_text_metrics.py#L459-L466) 过滤任务目录、计算镜像相对路径 `rel` 与数据集名；
- [L468-L474](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/evaluate_text_metrics.py#L468-L474) 同名约定 `{pf.stem}_qa_pred_masks.npy` 与逐位对齐的样本过滤；
- [L475-L492](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/evaluate_text_metrics.py#L475-L492) 逐 QA 判定：注意 [L487](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/evaluate_text_metrics.py#L487) `delta_ok = (res is not None and res["delta_success"] >= 1.0)`——要求该 QA **全部**数值对通过 δ，任何一对超差即整条失败；抽取仍复用 ape 的提示与函数（[L481-L485](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/evaluate_text_metrics.py#L481-L485)）。
- [L493-L507](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/evaluate_text_metrics.py#L493-L507) 汇总 per `(dataset, task)` 与 overall；一组都没 join 上时在 `error` 键里给出操作指引。

CLI 侧：[L539-L541](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/evaluate_text_metrics.py#L539-L541) 的 `--stages` 默认 `ape,judge`，**`all` 也只等于 `{ape, judge}`**——gmdelta 必须显式写出，且强制要求 `--mask_dump_dir`（[L577-L579](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/evaluate_text_metrics.py#L577-L579)）。两套端到端命令见 [README.md:L300-L316](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L300-L316)（Step 3b）。

#### 4.4.4 代码实践

**实践目标**：零 GPU 验证 GM-δ 的 join 契约与合取判定（用桩替换 LLM）。

1. 操作步骤（**示例代码**，临时目录运行）：

   ```python
   # gm_join_test.py — 示例代码：构造镜像树 + 桩 LLM，跑 stage_gmdelta
   import json, tempfile, sys
   from pathlib import Path
   import numpy as np
   sys.path.insert(0, "<仓库路径>")
   import tools.evaluate_text_metrics as etm

   root = Path(tempfile.mkdtemp())
   pred_dir = root / "val/scannet/part/distance_estimation"      # 预测树
   mask_dir = root / "masks/scannet/part/distance_estimation"    # 镜像树（结构相同）
   pred_dir.mkdir(parents=True); mask_dir.mkdir(parents=True)

   q = "How far is the chair from the camera?"
   (pred_dir / "scene0000_00.json").write_text(json.dumps(
       {"distance_estimation": [{"question": q, "gt_answer": "0.9m", "pred_answer": "1.0m"}]}))
   N = 6
   gt = np.zeros((1, N), bool); gt[0, :3] = True                  # GT：前 3 个点
   pr = np.zeros((1, N), bool); pr[0, :3] = True                  # 预测：完美 IoU=1.0
   np.save(mask_dir / "scene0000_00_qa_gt_masks.npy",   np.array([gt], dtype=object), allow_pickle=True)
   np.save(mask_dir / "scene0000_00_qa_pred_masks.npy", np.array([pr], dtype=object), allow_pickle=True)

   class StubLLM:                                                # 桩：直接返回规定形状的抽取 JSON
       def chat(self, system, user, max_new_tokens=512):
           return ('{"extracted_gt": {"distance_m": 0.9}, '
                   '"extracted_pred": {"distance_m": 1.0}}')

   out = etm.stage_gmdelta(pred_dir.parent.parent, mask_dir.parent.parent, StubLLM(),
                           {"distance_estimation"}, iou_thr=0.3, tau=1.25)
   print(json.dumps(out, indent=2, default=str))
   ```

   注意：`--input` 传的是 `val/` 这一级（预测树根），掩码树根 `masks/` 与之逐层镜像；`stage_gmdelta` 内部会用 `rglob` 重新发现 `distance_estimation` 目录。
2. 需要观察的现象：输出包含 `per_group["scannet/distance_estimation"]`，三项计数 `n=1`；`mask_iou>thr_rate=1.0`、`delta<=tau_rate=1.0`、`GM_delta_success_rate=1.0`。
3. 预期结果：IoU \(= 3/3 = 1.0 > 0.3\)，\(\delta = 1.0/0.9 = 1.111 \le 1.25\)，合取成功。若把桩中 pred 改成 `3.5`，则 `delta<=tau_rate` 与 `GM_delta_success_rate` 应变为 `0.0` 而 `mask_iou>thr_rate` 保持 `1.0`——精确展示「两种偏科」如何被拆开报告。
4. 本实践纯 CPU（LLM 被桩替换、IoU/δ 走真实代码路径）；`np.save` 的 object 数组形态对齐 [uniseg3d.py:L1321-L1325](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1321-L1325) 的落盘格式。运行结果**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：一条 QA 的掩码 IoU = 0.8（很好），但三个数值对中两个 δ 通过、一个 \(\delta = 1.4\)，GM-δ 记成功吗？
**答案**：不记。[L487](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/evaluate_text_metrics.py#L487) 要求 `delta_success >= 1.0`（QA 内**全部**对通过），任何一个超差整条失败。GM-δ 是合取语义，不容忍短板。

**练习 2**：预测目录在 `val/scannet/...` 而掩码被误存到 `masks/pred_qa_data_val/scannet/...`（多了一层），会发生什么？
**答案**：镜像路径 `mdir = mask_dump_dir / rel` 计算出的目录下找不到 `{scene}_qa_{pred,gt}_masks.npy`，[L470-L471](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/tools/evaluate_text_metrics.py#L470-L471) 会逐场景 `continue`，最终 `tot["n"] == 0`，输出 `error` 键提示重跑 `--mask-dump`。join 是纯字符串契约，**没有模糊匹配**。

**练习 3**：为什么 `_mask_iou` 与 u6-l1 的 `get_iou` 都要剔除「GT 行全 0」的行？
**答案**：全 0 行对应没有接地的 GT（如 `existence_verification` 的纯是/否答案，GT 是 NaN 哨兵落到全 0），它与任何预测的 IoU 都无意义。剔除后这类样本不进入 GM-δ 分母——文本与掩码两条通道的「有效样本」定义保持一致。

## 5. 综合实践

**任务：用「桩 LLM + 伪造产物」离线复现整条文本评测链路，并对照三个阶段的报告语义。**

背景：三个阶段共享同一套展平/抽取/配对/指标函数，真正的差别只在「过滤范围」与「汇总口径」。把它们放在同一场离线演练里对照，能一次看清分工。

步骤（**示例代码**，在仓库外的临时目录完成；不要向仓库写任何文件）：

1. 构造一个预测树，含**两个任务、两个场景**：

   ```text
   fake/pred/scannet/part/distance_estimation/sceneA.json   # 1 条 QA，gt 0.9 / pred 1.0
   fake/pred/scannet/part/scale_estimation/sceneB.json      # 1 条 QA，三维 gt [1.78,0.78,0.53] / pred [2.1,0.9,0.7]
   fake/mask/scannet/part/distance_estimation/sceneA_qa_{pred,gt}_masks.npy   # IoU=1.0
   fake/mask/scennet/part/...                                            # 故意拼错的数据集目录，验证发现机制
   ```

2. 写一个 `StubLLM`（同 4.4.4），让它按 `user` 串里的 `Task:` 字段返回对应形状的抽取 JSON。
3. 依次调用 `etm.gather_samples(...)`、`etm.stage_ape(samples, StubLLM(), 1.25)`、`etm.stage_gmdelta(pred_root, mask_root, StubLLM(), etm.NUMERIC_TASKS)`，把三份结果 `json.dumps` 打印。
4. 核对四个断言（这就是本实践的「验收标准」）：
   - `gather_samples` 返回 2 条样本，`_task` 分别为两个任务名；
   - `stage_ape.per_task` 中 `scale_estimation` 的 `APE_percent ≈ 21.81`、`delta@1.25_success_rate = 0.6667`（与 `--selftest` 的 T2 一致）；
   - `stage_gmdelta.per_group` 只有 `scannet/distance_estimation` 一组——拼错的 `scennet` 目录（掩码缺失）被逐场景跳过；
   - `judge` 阶段不做（需要真 VL 模型）；作为替代，用 `build_judge_user` 打印两条样本的完整判分 prompt，人工核对 `metric_accuracy` 分档描述是否出现。
5. 最后跑一次 `python tools/evaluate_text_metrics.py --selftest` 收尾，确认参考夹具全 OK。

预期结果：整条链路无 GPU 完成，三份报告的数值与第 4 步断言一致。真实 LLM 版本（`--stages ape`、`--stages judge`、`--stages gmdelta`）的运行属于**待本地验证**——需要按 [README.md:L300-L316](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/README.md#L300-L316) 先用 `--mask-dump` 重跑数值子任务的推理。

## 6. 本讲小结

- `tools/evaluate_text_metrics.py` 是与 mIoU **完全独立**的第二评测通道，靠 `--stages` 选阶段；默认 `ape,judge`，gmdelta 必须显式开启且强制 `--mask_dump_dir`。
- **任务感知的递归展平**（`flatten_with_task` + `gather_samples`）把任意嵌套/多轮/多子目录的预测 JSON 变成带 `_task` 标签的扁平样本列表，标签取值「JSON 内部 `meta_info.group` → 最近包裹键 → 路径目录名」三级回退；坏 JSON 告警跳过不中断。
- **ape 阶段**：文本 LLM 按任务形状抽取数字 → `fill_missing_with_zero` 强制罚 0（APE=100%、δ=∞、成功=0）→ GT==0 的对跳过 → QA 内对 pairs 求均值、任务内对 QA 宏观平均、overall 池化平均；数学实现可用 `--selftest` 零成本验证。
- **judge 阶段**：Qwen3-VL-4B-Instruct 对全部任务打 1-10 分，最终只聚合幻觉与完整性两维；判分失败/字段非数值的样本静默跳过；注意 `clean_text` 定义了但未接线，judge 看到的是含特殊标记的原始答案。
- **GM-δ**：掩码 IoU > 0.3 **且**该 QA 全部数值对 δ ≤ 1.25 才算成功；join 依赖「目录镜像 + 场景同名 + 样本按位对齐」的纯字符串契约，任何一环断裂只会得到 `error` 提示而非模糊匹配。
- 工程启示：能量化的（数值误差）用硬公式 + 单元测试；难量化的（幻觉/完整性）才交给 LLM 裁判，且用贪心解码保证可复现。

## 7. 下一步学习建议

- **u6-l4（接地可视化）**：本讲的 `--mask-dump` 产物（`qa_pred_masks.npy`）同样喂给 `tools/export_grounding_ply.py` 与 `uniseg3d/grounding_viz.py`——学完 GM-δ 再看如何把 `<SEG>` 接地染成彩色 PLY 点云，能把「掩码通道」的下游一次补全。
- 若你关心**指标的对偶侧**：回头对照 u6-l1 的 `get_iou`（`uniseg3d/unified_metric.py`）与本讲 `_mask_iou`，体会「两处实现同一口径」的取舍。
- 想动手改进的读者可以思考两个小课题（本地实验）：把 `clean_text` 接进 judge 输入后幻觉分是否变化；给 `parse_extraction` 的正则兜底扩展到 `scale_estimation`。两者都不需要训练模型。
- 最后一站是 u7 单元（ScanRefer/Reason3D 迁移、二次开发与架构取舍），届时本讲的评测链路会成为「新基准怎么接」的模板。
