# 奖励模型 rm_hub 与内置奖励类型

## 1. 本讲目标

在 slime 的 RL 闭环里，「模型生成一条回答」之后必须立刻回答一个问题：**这条回答值多少分？** 这个分数（reward）会写进 `Sample.reward`，再被优势估计器（advantage estimator）转换成训练信号。本讲要讲清楚的就是负责「算分」的这一层——`rm_hub`。

读完本讲你应该能够：

1. 说清 `async_rm` 如何按「逐样本自定义路径 → 全局自定义路径 → 内置 rm_type」三级优先级分发奖励计算。
2. 读懂 `boxed_` 前缀的含义，以及 `extract_boxed_answer` 如何用括号匹配从 LaTeX 文本里抠出最终答案。
3. 读懂 `get_deepscaler_rule_based_reward` 这条「思考模型」专用数学判等链路，以及它和纯 `math` 判等的差别。
4. 区分 `--custom-rm-path`（单样本 / batch 两种签名）与 `--group-rm` 两种奖励调用模式，并能自己写一个可被 rm_hub 调起的奖励函数。

## 2. 前置知识

- **奖励（reward）在闭环中的位置**：rollout 阶段每生成完一条 `Sample`，框架都会调用奖励逻辑算一个标量（或 dict）写回 `sample.reward`。这个值随后流入训练侧，在同一个 prompt 组内做归一化得到优势（advantage）。以 GRPO 为例，组内奖励会被归一化为：

  \[
  A_i = \frac{r_i - \mathrm{mean}(\mathbf{r})}{\mathrm{std}(\mathbf{r}) + \epsilon}
  \]

  所以奖励的**相对大小**比绝对大小更重要。这也是为什么 slime 默认的内置奖励大多是简单的 0/1 二值（答对得 1，答错得 0）——只要组内有对有错，归一化后就能产生有效梯度。优势估计的细节见 u6-l4。

- **Sample 数据结构**：奖励逻辑会读取 `sample.prompt`、`sample.response`（模型生成的文本）、`sample.label`（标准答案）、`sample.metadata`（可携带 `rm_type` 等元信息）、`sample.custom_rm_path`（逐样本覆盖奖励路径）。这些字段在 u3-l1 已详述。

- **load_function 机制**：slime 把「import 路径字符串」解析成可调用对象的统一工具，是所有 `--xxx-path` 接口的底座。它的实现极简——按最后一个 `.` 切分模块名与属性名，`importlib.import_module` 后 `getattr`。

- **rule-based reward vs reward model**：本讲聚焦的 `rm_hub` 主要是**规则奖励**（rule-based），即用确定性的 Python 代码判定答案对错；唯一的例外是 `remote_rm`，它把请求转发给外部奖励服务（可以是真正的神经网络奖励模型）。

## 3. 本讲源码地图

| 文件 | 作用 |
| :--- | :--- |
| `slime/rollout/rm_hub/__init__.py` | 奖励分发中枢：`async_rm`（单样本）、`batched_async_rm`（整组）、`remote_rm`（远程服务），以及按 `rm_type` 的 if 分发。 |
| `slime/rollout/rm_hub/deepscaler.py` | 思考模型数学奖励 `get_deepscaler_rule_based_reward`：处理 `</think>` 标签、抽答案、判等。 |
| `slime/rollout/rm_hub/math_utils.py` | 数学答案的底层工具：`extract_answer`/`extract_boxed_answer` 抽取、`grade_answer_mathd`（字符串级）/`grade_answer_sympy`（符号级）判等、`grade_answer_verl` 组合入口。 |
| `slime/rollout/sglang_rollout.py` | 调用方：`generate_and_rm` 与 `generate_and_rm_group` 决定何时、以单样本还是整组方式调用 rm_hub。 |
| `slime/utils/arguments.py` | 定义 `--rm-type`、`--group-rm`、`--rm-url`、`--custom-rm-path` 等参数。 |
| `slime/utils/misc.py` | `load_function`：把路径字符串解析成函数对象。 |

## 4. 核心概念与源码讲解

### 4.1 async_rm：奖励分发的总入口

#### 4.1.1 概念说明

`async_rm` 是所有奖励计算的**单样本总入口**。无论奖励最终用哪种算法算出来，rollout 侧都会先走进这个函数。它的核心职责不是「算分」，而是**分发**——根据一组优先级规则，决定把当前 `sample` 交给谁去算。

为什么需要分发而不是直接写死一个判等函数？因为 slime 要支持极其多样的奖励来源：数学题答案对错、F1 分数、GPQA 选择题、外部神经网络打分服务、用户自己写的任意逻辑……与其在框架里枚举所有任务，不如用一个统一的分发器把「选哪条算分路径」这件事集中起来，剩下的算分细节下放到各个内置模块或用户自定义函数。

#### 4.1.2 核心流程

`async_rm(args, sample, **kwargs)` 的分发按**优先级从高到低**判断，命中即返回：

```text
1. sample.custom_rm_path 存在？   → 是：load_function 加载并调用，返回（最高优先级，逐样本覆盖）
2. args.custom_rm_path 存在？     → 是：load_function 加载并调用，返回（全局自定义）
3. 解析 rm_type：
   rm_type = sample.metadata["rm_type"] 或 args.rm_type 或 ""
   若以 "boxed_" 开头：先用 extract_boxed_answer 抽取 response，再去掉前缀
4. 按 rm_type 字符串分发：
   remote_rm / deepscaler / dapo / math / f1 / gpqa / ifbench / random
   未实现的 rm_type → NotImplementedError
   空 rm_type        → NotImplementedError（必须显式指定）
```

两条关键约定：

- **逐样本优先于全局**：`sample.custom_rm_path`（来自 eval 数据集配置，见 `sglang_rollout.py` 里 `sample.custom_rm_path = dataset_cfg.custom_rm_path`）压过 `args.custom_rm_path`。这让评估阶段可以给不同数据集配不同奖励，而不影响训练。
- **metadata 优先于 args**：`rm_type` 的解析是 `metadata.get("rm_type") or args.rm_type`，同样让逐样本的元信息压过全局参数。

#### 4.1.3 源码精读

分发主体的三段优先级判断，逻辑非常直白：

逐样本自定义路径优先（最高优先级）—— [slime/rollout/rm_hub/\_\_init\_\_.py:L55-L59](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/rm_hub/__init__.py#L55-L59)：如果 `sample.custom_rm_path` 有值，就用 `load_function` 把这个 import 路径解析成函数对象并 `await` 调用，立即返回，不再走后面的 rm_type 逻辑。

全局自定义路径次之 —— [slime/rollout/rm_hub/\_\_init\_\_.py:L61-L63](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/rm_hub/__init__.py#L61-L63)：同样的 `load_function` + `await`，区别只是来源是命令行 `--custom-rm-path`。

`load_function` 本身极简 —— [slime/utils/misc.py:L39-L47](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/misc.py#L39-L47)：按最后一个点把 `"a.b.c"` 切成模块路径 `a.b` 和属性名 `c`，`importlib.import_module("a.b")` 后 `getattr(module, "c")`。理解了这个，你就理解了 slime 所有 `--xxx-path` 接口的本质：它们都是把字符串翻译成函数对象的统一约定。

rm_type 解析与 boxed_ 前缀处理 —— [slime/rollout/rm_hub/\_\_init\_\_.py:L65-L71](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/rm_hub/__init__.py#L65-L71)：先取 `rm_type`（metadata 优先于 args），再判断是否以 `boxed_` 开头。若是，先把 `response` 换成「只保留 `\boxed{}` 内容」的结果，再去掉前缀（下文 4.2 详述）。

按 rm_type 字符串分发到各内置奖励 —— [slime/rollout/rm_hub/\_\_init\_\_.py:L75-L96](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/rm_hub/__init__.py#L75-L96)：一串 `if rm_type == ...: return ...`。注意几个细节：`remote_rm` 走 HTTP；`deepscaler`/`dapo`/`math` 都是数学判等的不同变体；`f1` 取 `f1_score(...)[0]`（F1 元组的首元素）；`gpqa`/`ifbench` 需要透传 `metadata`；`random` 返回 0 或 1，是测试用的占位；**未指定 rm_type 会直接抛 `NotImplementedError`**——框架拒绝「猜」你要怎么打分。

> 内置 rm_type 完整清单（与官方文档一致）：`math` / `dapo` / `deepscaler` / `f1` / `gpqa` / `ifbench` / `remote_rm` / `random`。

#### 4.1.4 代码实践

**实践目标**：用 `async_rm` 跑通内置 `random` 奖励，确认它能返回 0 或 1，并体会「不指定 rm_type 会报错」。

**操作步骤**：

1. 写一个最小脚本（示例代码）：

   ```python
   # play_async_rm.py（示例代码）
   import asyncio
   from types import SimpleNamespace
   from slime.utils.types import Sample
   from slime.rollout.rm_hub import async_rm

   async def main():
       args = SimpleNamespace(
           rm_type="random", custom_rm_path=None, rm_url=None
       )
       s = Sample(index=0, response="anything", label="")
       print("random 奖励 =", await async_rm(args, s))

       # 不指定 rm_type，应当抛 NotImplementedError
       bad = SimpleNamespace(rm_type=None, custom_rm_path=None, rm_url=None)
       try:
           await async_rm(bad, s)
       except NotImplementedError as e:
           print("预期的报错：", e)

   asyncio.run(main())
   ```

2. 运行：`python play_async_rm.py`。

**需要观察的现象**：

- `random 奖励 =` 后面是 0 或 1（随机）。
- 第二段打印出 `预期的报错： Rule-based RM type is not specified.`。

**预期结果**：能成功调用 `async_rm` 并看到上述两条输出。

**待本地验证**：`random.randint(0, 1)` 的具体值每次不同，属正常。

#### 4.1.5 小练习与答案

**练习 1**：如果一个数据集的每条样本 `metadata = {"rm_type": "math"}`，同时命令行又设了 `--rm-type deepscaler`，最终用哪个判等器？

**答案**：用 `math`（即 `grade_answer_verl`）。因为 rm_type 解析是 `metadata.get("rm_type") or args.rm_type`，逐样本 metadata 优先。

**练习 2**：当 `--custom-rm-path` 与 `--rm-type` 同时设置时，哪个生效？

**答案**：`--custom-rm-path` 生效。在 `async_rm` 中，`args.custom_rm_path is not None` 的判断在 rm_type 分发**之前**，一旦命中就 `return`，根本走不到 rm_type 那串 if。

---

### 4.2 extract_boxed_answer 与 boxed_ 前缀：从 LaTeX 里抠答案

#### 4.2.1 概念说明

数学/推理模型的回答通常是一大段推理过程加上一个被 `\boxed{...}` 包裹的最终答案。奖励函数只想判这个「最终答案」对不对，不想被推理文本干扰。`extract_boxed_answer` 就是「从 LaTeX 文本里把最后一个 `\boxed{}` 的内容抠出来」的工具。

它和 `boxed_` 前缀配合使用：当 `rm_type` 形如 `boxed_f1` 时，`async_rm` 会**先**对 response 调一次 `extract_boxed_answer`，把整段回答替换成裸答案，再交给 `f1` 打分。这样像 `f1`、`gpqa` 这类「拿 response 整段文本去比」的判分器也能处理「模型把答案藏在 `\boxed{}` 里」的情况。

#### 4.2.2 核心流程

抽取分两步，都基于**括号匹配计数**（而非正则），因为 `\boxed{}` 内容里可能嵌套大括号：

```text
last_boxed_only_string(s):       # 在 s 里找到最后一个 "\boxed{...}"（或 "\fbox{...}"）的完整子串
    从右往左找最后一个 "\boxed"
    从该位置起逐字符扫描，{ 计数 +1，} 计数 -1
    计数第一次回到 0 的位置就是闭合括号
    返回 s[start : close+1]      # 形如 "\boxed{1+1}"，找不到返回 None

remove_boxed(s):                  # 去掉外壳 "\boxed{" 和结尾 "}"
    断言以 "\boxed{" 开头、以 "}" 结尾
    返回中间内容；断言失败返回 None

extract_boxed_answer(solution):  # 组合：先定位再剥壳
    return remove_boxed(last_boxed_only_string(solution))
```

注意 `remove_boxed` 用的是 `try/except` 包裹的 `assert`：若格式不对（比如 `\boxed` 后没有配对括号），返回 `None` 而非抛错，保证奖励流程不会因一条畸形回答崩溃。

#### 4.2.3 源码精读

定位最后一个 `\boxed` 完整子串 —— [slime/rollout/rm_hub/math_utils.py:L384-L409](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/rm_hub/math_utils.py#L384-L409)：先 `rfind("\\boxed")`（找不到再退而求其次找 `\\fbox`），再用一个 while 循环做括号深度计数，深度归零即闭合。`rfind` 保证取**最后一个** `\boxed`——因为模型可能在思考过程里写过多个，只有最后那个才是最终答案。

剥掉外壳 —— [slime/rollout/rm_hub/math_utils.py:L412-L419](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/rm_hub/math_utils.py#L412-L419)：断言首尾符合 `\boxed{ ... }` 结构后切掉前后缀，返回裸内容。

组合入口与 `extract_answer` —— [slime/rollout/rm_hub/math_utils.py:L422-L426](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/rm_hub/math_utils.py#L422-L426) 与 [slime/rollout/rm_hub/math_utils.py:L478-L481](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/rm_hub/math_utils.py#L478-L481)：`extract_boxed_answer` 直接组合两步；`extract_answer(passage)` 则多一层判断——只有当 passage 里确实含 `\boxed` 时才抽取，否则返回 `None`。`extract_answer` 是 `deepscaler` 和 `grade_answer_verl` 实际调用的入口。

`boxed_` 前缀在哪里被消费 —— [slime/rollout/rm_hub/\_\_init\_\_.py:L69-L71](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/rm_hub/__init__.py#L69-L71)：`response = extract_boxed_answer(response) or ""`，再把 `rm_type` 从 `boxed_xxx` 裁成 `xxx`。注意 `or ""`：抽不到时退化为空串，后续判分自然得 0 分。

> **一个要避坑的点**：`deepscaler` 和 `math` 这两个 rm_type **内部已经自己抽 `\boxed` 了**（见 4.3）。所以 `boxed_deepscaler`、`boxed_math` 实际是重复抽取，对它们意义不大；`boxed_` 前缀真正有用的是面向 `f1`、`gpqa` 这类不自抽答案的判分器。

#### 4.2.4 代码实践

**实践目标**：手动验证 `extract_boxed_answer` 对嵌套括号和多个 `\boxed` 的行为。

**操作步骤**（示例代码）：

```python
from slime.rollout.rm_hub.math_utils import extract_boxed_answer, extract_answer

# 情况 1：嵌套大括号
print(extract_boxed_answer(r"The answer is \boxed{\frac{1}{2}}."))   # 期望 \frac{1}{2}

# 情况 2：多个 boxed，只取最后一个
print(extract_boxed_answer(r"\boxed{wrong} then \boxed{42}"))        # 期望 42

# 情况 3：没有 boxed
print(extract_answer("no box here"))                                  # 期望 None
```

**需要观察的现象**：

- 情况 1 输出 `\frac{1}{2}`，证明嵌套括号被正确闭合。
- 情况 2 输出 `42`，证明取的是最后一个。
- 情况 3 输出 `None`。

**预期结果**：上述三行输出依次为 `\frac{1}{2}`、`42`、`None`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `last_boxed_only_string` 用括号计数而不用正则 `r"\\boxed\{(.+)\}"`？

**答案**：因为 `\boxed{}` 内容里会嵌套大括号（如 `\boxed{\frac{1}{2}}`），正则的贪婪/非贪婪匹配很难正确处理任意深度嵌套；括号深度计数是唯一稳健的办法。

**练习 2**：`boxed_` 前缀被去掉后，rm_type 变成什么？如果 `rm_type="boxed_"`（前缀后为空），会发生什么？

**答案**：`rm_type = rm_type[len("boxed_"):]`。若剥离后为空串，会落到 `async_rm` 最后的 `else` 分支抛 `NotImplementedError("Rule-based RM type is not specified.")`。

---

### 4.3 get_deepscaler_rule_based_reward 与数学判等

#### 4.3.1 概念说明

`get_deepscaler_rule_based_reward` 是 slime 给**带思考过程的数学模型**（如 DeepSeek-R1、GLM-4.x thinking）准备的规则奖励。它返回**二值**结果：答对 1，答错 0。

它和普通 `math` 判等（`grade_answer_verl`）最大的区别在于：它会先处理 `</think>` 标签——思考型模型把推理放在 `<think>...</think>` 里，真正的解答在标签之后。`deepscaler` 只在「思考之后的正文」里找答案，避免把思考过程中的草稿当成最终答案。

判等的难点是「答案形式不同但数学等价」，例如 `\frac{1}{2}` 与 `0.5`、`1/2` 与 `0.5`。slime 用两条判等路径兜底：字符串级归一化比较（`grade_answer_mathd`）+ sympy 符号化简比较（`grade_answer_sympy`），任一通过即算对。

#### 4.3.2 核心流程

`get_deepscaler_rule_based_reward(response, label)`：

```text
1. 定位模型解答正文：
   若含 "</think>"：取其之后的部分（跳过思考）
   否则若含 "###Response"：取其之后的部分
   否则：return 0（找不到解答段）
2. model_answer = extract_answer(正文)   # 抠 \boxed
   若 model_answer is None：return 0
   若 label == ""：return 0
3. 处理 label：若 label 含 \boxed 则同样抽取，否则原样使用
4. 判等：grade_answer_mathd(model_answer, truth) or grade_answer_sympy(model_answer, truth)
   任一为 True → return 1
   全 False   → return 0
```

两条判等路径：

- `grade_answer_mathd`：对双方做 `mathd_normalize_answer`（一种激进的字符串清洗：去空格、统一 `\frac` 写法、`0.5→\frac{1}{2}` 等），然后**严格字符串相等**。
- `grade_answer_sympy`：对双方做 `_normalize`（更复杂的归一化，含单位剥离、LaTeX→文本），再尝试用 sympy 化简 `(truth)-(given)` 是否为 0。能识别 `\frac{1}{2} == 0.5` 这类等价。

`grade_answer_sympy` 里有安全护栏 `should_allow_eval`：当表达式含未知字母过多、或形如 `^{...}`、`^(...` 等会让 sympy「挂起」的子串时，直接放弃符号比较（避免一条畸形答案卡死整个奖励计算）。

#### 4.3.3 源码精读

`get_deepscaler_rule_based_reward` 主体 —— [slime/rollout/rm_hub/deepscaler.py:L4-L42](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/rm_hub/deepscaler.py#L4-L42)：先按 `</think>` / `###Response` 切出正文（两处都没有就直接返回 0）；再抽 `model_answer`；再处理 `label`（注意它把 label 统一包成 list 再循环，是为未来支持多个等价答案预留的结构，目前只有一个元素）；最后用 mathd 与 sympy 双重判等，**短路或**——一个对就算对。

字符串级判等 —— [slime/rollout/rm_hub/math_utils.py:L468-L475](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/rm_hub/math_utils.py#L468-L475)：`grade_answer_mathd` 调 `mathd_normalize_answer`（[math_utils.py:L15-L26](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/rm_hub/math_utils.py#L15-L26)，剥 `\text{}` 后做 `_strip_string` 清洗），再比字符串。`_strip_string`（[math_utils.py:L29-L159](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/rm_hub/math_utils.py#L29-L159)）是这套清洗的核心，把 `tfrac/dfrac` 统一成 `frac`、`a/b` 改写成 `\frac{a}{b}`、去掉 `\left \right`、统一 `0.5` 等。

符号级判等 —— [slime/rollout/rm_hub/math_utils.py:L429-L465](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/rm_hub/math_utils.py#L429-L465)：`grade_answer_sympy` 先各自 `_normalize`，相等就直接 True；否则按元组/单元素拆分后逐对比较，最终落到 `are_equal_under_sympy`（[math_utils.py:L351-L362](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/rm_hub/math_utils.py#L351-L362)）：构造 `(truth)-(given)`，过 `should_allow_eval` 护栏后用 sympy `simplify`，结果为 0 即等价。

安全护栏 —— [slime/rollout/rm_hub/math_utils.py:L328-L348](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/rm_hub/math_utils.py#L328-L348)：`should_allow_eval` 拒绝未知字母过多（`count_unknown_letters_in_expr > 2`）或含 `^{`/`^(`/`^^` 等危险子串的表达式，防止 sympy 在畸形输入上无限化简。此外 `_sympy_parse`（[math_utils.py:L168-L179](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/rm_hub/math_utils.py#L168-L179)）用白名单 `safe_dict` 限制可调符号，避免任意代码执行——这是把不可信的模型输出喂给 sympy 时的必要防护。

> `rm_type="math"` 走的 `grade_answer_verl`（[math_utils.py:L484-L493](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/rm_hub/math_utils.py#L484-L493)）判等逻辑与 deepscaler 的第 4 步**完全相同**（都是 mathd or sympy），区别仅在于它**不切 `</think>`**，直接在整个 `solution_str` 里抽 `\boxed`。所以：思考模型用 `deepscaler`，非思考模型用 `math`。

#### 4.3.4 代码实践

**实践目标**：直接调用 `get_deepscaler_rule_based_reward`，验证它能识别等价答案，并体会 `</think>` 切分的作用。

**操作步骤**（示例代码）：

```python
from slime.rollout.rm_hub.deepscaler import get_deepscaler_rule_based_reward

# 情况 1：思考过程里写了错误答案，正文里写了对的 —— 应判对
r1 = "<think>let me try 3</think>The answer is \\boxed{42}."
print(get_deepscaler_rule_based_reward(r1, "42"))           # 期望 1

# 情况 2：等价但写法不同 —— \frac{1}{2} vs 0.5
r2 = "<think>...</think>\\boxed{\\frac{1}{2}}"
print(get_deepscaler_rule_based_reward(r2, "0.5"))          # 期望 1

# 情况 3：答错
r3 = "<think>...</think>\\boxed{7}"
print(get_deepscaler_rule_based_reward(r3, "42"))           # 期望 0
```

**需要观察的现象**：三行输出依次为 `1`、`1`、`0`。第 2 行若为 0，说明本地 sympy 版本对该等价的化简行为不同。

**预期结果**：`1 / 1 / 0`。

**待本地验证**：情况 2 依赖 sympy 对 `\frac{1}{2}` 与 `0.5` 的化简，不同 sympy 版本可能表现不一；若得 0 属环境差异。

#### 4.3.5 小练习与答案

**练习 1**：模型回答里根本没有 `</think>` 也没有 `###Response`，`get_deepscaler_rule_based_reward` 返回什么？为什么？

**答案**：返回 0。因为第一步定位正文时两处标记都不命中，直接 `return 0`，连答案抽取都不会做。这正是 `deepscaler` 强假设「回答必须有可识别的解答段」的体现。

**练习 2**：为什么判等要同时用 `grade_answer_mathd` 和 `grade_answer_sympy`，而不是只用 sympy？

**答案**：两者各有长短。`mathd` 是纯字符串比较，快且确定，能处理 sympy 不擅长的情形；`sympy` 能识别数学等价（`\frac{1}{2}==0.5`），但会被畸形输入卡住甚至挂起（故有 `should_allow_eval` 护栏）。短路或的组合兼顾覆盖率和鲁棒性。

---

### 4.4 custom_rm_path 与 group_rm：两种奖励调用模式

#### 4.4.1 概念说明

内置 rm_type 覆盖的是「答案对错」这类通用场景。当你想算的奖励是内置没有的——比如「响应长度」「是否调用了正确工具」「代码能否通过单元测试」「外部打分服务」——就用 `--custom-rm-path` 注入自己的函数。

奖励函数有**两种签名**，对应两种调用模式：

- **单样本模式（默认）**：`async def rm(args, sample) -> float`，一次算一条。slime 在 rollout 内逐样本（或对一批样本逐条 gather）调用。
- **batch / group 模式（`--group-rm`）**：`async def rm(args, samples) -> list[float]`，一次接收整个 prompt 组。适用于「奖励需要同组对比」或「外部服务按批调用更高效」的场景。

`--group-rm` 不只是签名不同，它还改变了 rollout 的**调用时机**：开启后，`generate_and_rm` 会在生成完就提前返回、**不算奖励**，把算奖励的职责上移到 `generate_and_rm_group`，等整组生成完毕后一次性算。

#### 4.4.2 核心流程

两种模式在 `sglang_rollout.py` 里的调用点：

```text
# 默认（非 group_rm）
generate_and_rm(单样本):
    ...生成...
    if args.group_rm: return sample           # 开了 group_rm 就这里不算，提前返回
    if sample.reward is None:
        sample.reward = await async_rm(args, sample)        # 逐样本
generate_and_rm_group(整组):
    group = await gather(...)                 # 并发生成整组
    if args.group_rm:
        rewards = await batched_async_rm(args, group)       # 整组一次性算
        逐个写回 sample.reward

# batched_async_rm 的内部分发：
def batched_async_rm(args, samples):
    if args.custom_rm_path:                   # 全局自定义：要求 batch 签名
        return await load_function(args.custom_rm_path)(args, samples)
    else:                                     # 否则退化为逐样本 gather
        return await gather(*(async_rm(args, s) for s in samples))
```

一个**关键细节**：`batched_async_rm` 只检查 `args.custom_rm_path`（全局），**不检查** `sample.custom_rm_path`（逐样本）。所以逐样本级别的自定义奖励只在单样本 `async_rm` 路径里生效；进入 batch 路径后逐样本覆盖会被忽略。这也是为什么 eval 路径里 `--group-rm` 被显式禁止（`assert not args.group_rm`）。

#### 4.4.3 源码精读

默认逐样本调用点 —— [slime/rollout/sglang_rollout.py:L283-L285](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_rollout.py#L283-L285)：当 `sample.reward` 仍为 `None`（即 custom_generate 没顺手填好奖励）时，调用 `async_rm(args, sample)`。注意上面 L265-L266 的提前返回：开了 `group_rm` 时这里直接 `return sample`，把算分让出去。

整组调用点 —— [slime/rollout/sglang_rollout.py:L328-L332](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_rollout.py#L328-L332)：`generate_and_rm_group` 在整组 `gather` 完成后，若 `group_rm` 开启，调 `batched_async_rm(args, group)` 一次性算整组奖励并写回。

batch 入口的两种内部分发 —— [slime/rollout/rm_hub/\_\_init\_\_.py:L99-L110](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/rm_hub/__init__.py#L99-L110)：有全局 `custom_rm_path` 就要求该函数是 batch 签名（收 `samples` 列表）；否则 `asyncio.gather` 逐样本并发调 `async_rm`。这条「否则」分支说明：即使开了 `--group-rm` 但**没给** `--custom-rm-path`，内置 rm_type 仍按逐样本并发执行，group_rm 只影响「何时算」（整组生成后），不影响「怎么算」（仍是逐条）。

参数定义 —— [slime/utils/arguments.py:L1339-L1357](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L1339-L1357)：`--group-rm`（`store_true`，默认 False）、`--rm-url`（remote_rm 服务地址）、`--custom-rm-path`（help 文本明确写出单样本签名 `def custom_rm(args, sample) -> float`）。batch 签名见官方 customization 文档（`async def batched_custom_rm(args, samples) -> list[float]`）。

eval 路径禁止 group_rm —— [slime/rollout/sglang_rollout.py:L475](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_rollout.py#L475) 与 [sglang_rollout.py:L497](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_rollout.py#L497)：评估阶段两处 `assert not args.group_rm`，因为评估的指标统计假设逐样本奖励。

#### 4.4.4 代码实践

**实践目标**：写一个「响应长度相关」的 `custom_rm`，用单样本签名，并通过 `async_rm` 验证它能被 rm_hub 正确调起。

**操作步骤**：

1. 新建模块文件 `my_reward/length_rm.py`（示例代码）：

   ```python
   # my_reward/length_rm.py（示例代码，需放在可被 import 的路径上）
   from slime.utils.types import Sample

   async def rm(args, sample: Sample, **kwargs) -> float:
       """响应越接近 target_length 得分越高，偏离则线性衰减到 0。"""
       target = getattr(args, "target_length", 64)
       n = len(sample.response)
       score = max(0.0, 1.0 - abs(n - target) / target)
       return float(score)
   ```

2. 写一个本地验证脚本，**直接复用 rm_hub 的分发逻辑**（示例代码）：

   ```python
   # verify_length_rm.py（示例代码）
   import asyncio
   from types import SimpleNamespace
   from slime.utils.types import Sample
   from slime.rollout.rm_hub import async_rm

   async def main():
       # 用 --custom-rm-path 指向我们的函数，模拟真实接入
       args = SimpleNamespace(
           custom_rm_path="my_reward.length_rm.rm",
           rm_type=None, rm_url=None, target_length=10,
       )
       s = Sample(index=0, response="1234567890")  # 恰好 10 字符
       print("长度奖励 =", await async_rm(args, s))

   asyncio.run(main())
   ```

3. 确保 `my_reward` 可被 import（例如与脚本同目录，或设 `PYTHONPATH=.`），运行 `python verify_length_rm.py`。

**需要观察的现象**：`长度奖励 = 1.0`（响应正好 10 字符，与 `target_length=10` 重合，得满分）。把 `response` 改成 `"12345"`（5 字符），应输出 `0.5`；改成空串，输出 `0.0`。

**它如何被 rm_hub 调用（关键说明）**：

- 接入方式：训练命令加 `--custom-rm-path my_reward.length_rm.rm`。
- 运行时，rollout 在 `generate_and_rm` 里发现 `sample.reward is None`，调 `async_rm(args, sample)`。
- `async_rm` 命中**第二优先级** `args.custom_rm_path is not None`，用 `load_function("my_reward.length_rm.rm")` 把字符串解析成上面的 `rm` 函数对象并 `await rm(args, sample)`。
- 返回值写进 `sample.reward`，随后进入优势估计。

**进阶（group_rm 模式）**：若改用 `--group-rm`，同一函数签名会失效（`batched_async_rm` 传的是 `samples` 列表）。需改成 batch 签名：

```python
# 示例代码：batch 签名
async def rm(args, samples: list[Sample], **kwargs) -> list[float]:
    target = getattr(args, "target_length", 64)
    return [float(max(0.0, 1.0 - abs(len(s.response) - target) / target)) for s in samples]
```

**预期结果**：单样本模式验证脚本输出 `1.0`（10 字符）。

**待本地验证**：实际训练接入需 SGLang/Megatron 环境；本实践聚焦于「函数能被 rm_hub 正确解析与调用」这一点，可在纯 CPU + 已装 slime 的环境验证。

#### 4.4.5 小练习与答案

**练习 1**：如果你希望「同一个 `--custom-rm-path` 函数既能在默认模式工作、又能在 `--group-rm` 模式工作」，可行吗？

**答案**：不行，签名互斥。默认模式要求 `rm(args, sample)->float`，group 模式要求 `rm(args, samples)->list[float]`。`batched_async_rm` 在 `args.custom_rm_path` 命中时**强制**按 batch 调用（传列表）。要切换模式必须改函数签名并重启训练。

**练习 2**：`batched_async_rm` 在「开了 `--group-rm` 但没给 `--custom-rm-path`、只用内置 `--rm-type math`」时会怎么算？

**答案**：走 [rm_hub/\_\_init\_\_.py:L108-L110](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/rm_hub/__init__.py#L108-L110) 的 else 分支：`asyncio.gather` 对组内每个样本并发调 `async_rm`，各自按 `math` 判等。group_rm 只改变了「算奖励的时机」（整组生成后），没改变内置奖励的「逐样本计算方式」。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个「为数学题设计奖励配置」的小任务。

**场景**：你有一个思考型数学模型，输出形如 `<think>推理</think> 最终答案是 \boxed{...}`。你希望：

1. 答案正确得基础分 1.0（用内置判等）。
2. 额外奖励简洁回答：响应正文（不含 think）每多一个字符扣一点点分，但最终奖励仍以答对为前提。

**任务**：

1. **选择 rm 配置**：思考模型该用 `--rm-type deepscaler` 还是 `math`？为什么？（提示：看 `</think>` 切分。）
2. **写一个 custom_rm**：它要先复用 slime 的判等逻辑判断对错（提示：可直接 `from slime.rollout.rm_hub.deepscaler import get_deepscaler_rule_based_reward`），再叠加长度惩罚。要求签名 `async def rm(args, sample) -> float`。
3. **说明调用链**：用本讲学到的 `async_rm` 分发优先级，解释这个函数是如何被调起的、`sample.response` 与 `sample.label` 各自从哪来。
4. **本地验证**：用「4.4.4」那样的 `async_rm` 小脚本，构造一条答对但冗长的样本和一条答对且简洁的样本，观察奖励差异。

**参考思路（示例代码）**：

```python
# my_reward/deepscaler_with_length.py（示例代码）
from slime.utils.types import Sample
from slime.rollout.rm_hub.deepscaler import get_deepscaler_rule_based_reward

async def rm(args, sample: Sample, **kwargs) -> float:
    correct = get_deepscaler_rule_based_reward(sample.response, sample.label or "")
    if not correct:
        return 0.0
    # 答对才有资格拿长度分；正文越短越高
    body = sample.response.split("</think>")[-1] if "</think>" in sample.response else sample.response
    penalty = getattr(args, "length_penalty", 0.001)
    return max(0.0, 1.0 - penalty * len(body))
```

**预期结论**：

1. 选 `deepscaler`，因为它会切掉 `<think>` 段、只在正文判等；`math` 不切，会把思考里的草稿也算进去。
2. 调用链：rollout → `async_rm(args, sample)` → 命中第二优先级 `args.custom_rm_path` → `load_function` 解析 → `await rm(args, sample)` → 返回值写入 `sample.reward`。`sample.response` 来自 SGLang 生成并 decode 的文本，`sample.label` 来自数据源（DataSource 取 prompt 时一同携带的标准答案，见 u3-l3）。
3. 冗长样本奖励低于简洁样本，且答错样本恒为 0。

## 6. 本讲小结

- `async_rm` 是奖励分发的**单样本总入口**，按「逐样本 `sample.custom_rm_path` → 全局 `args.custom_rm_path` → 按 `rm_type` 内置分发」三级优先级命中即返回，未指定 rm_type 会抛 `NotImplementedError`。
- `rm_type` 同样是逐样本优先：`metadata["rm_type"]` 压过 `args.rm_type`；`boxed_` 前缀会先用 `extract_boxed_answer` 把 response 收窄到 `\boxed{}` 内容，再去掉前缀交给具体判分器。
- `extract_boxed_answer` 用**括号深度计数**定位最后一个 `\boxed{...}` 并剥壳，稳健处理嵌套大括号；取最后一个是因为只有最后的 `\boxed` 才是最终答案。
- `get_deepscaler_rule_based_reward` 是思考型数学模型的二值（0/1）奖励：切 `</think>` 取正文 → 抽答案 → 用 `grade_answer_mathd`（字符串归一化）`or` `grade_answer_sympy`（符号化简）双重判等；`math`（`grade_answer_verl`）判等逻辑相同但不切 think。
- 自定义奖励有两种签名：单样本 `async def rm(args, sample)->float`（默认）、batch `async def rm(args, samples)->list[float]`（`--group-rm`）；`group_rm` 还把算分时机从「生成即算」推迟到「整组生成后一次算」，且评估阶段被禁止。
- 所有 `--xxx-path` 接口的底座都是 `load_function`：把 import 路径字符串解析成函数对象——这是 slime 可扩展性的统一约定。

## 7. 下一步学习建议

- **奖励之后是什么**：奖励写进 `sample.reward` 后，会先经过 `--custom-reward-post-process-path`（默认是 GRPO 的组内归一化），再进训练侧优势估计。建议接着读 **u6-l4（优势估计器与 RL 算法选择）**，看 reward 如何变成 advantage。
- **奖励计算的上下游**：往上看，奖励在 `generate_and_rm` / `generate_and_rm_group` 里被调用，详见 **u3-l2（默认 rollout 全流程）**；往下看，样本如何被打包成训练张量，见 **u4-l3（数据打包与微批调度）**。
- **更多自定义接口**：本讲的 `custom_rm` 只是 slime 众多 `--xxx-path` 接口之一，完整的接口全景与契约测试见 **u6-l1（定制化接口总览）** 与 **u8-l6（测试与契约测试）**。
- **想接外部奖励服务**：若你的奖励是神经网络模型或在线服务，读 `remote_rm`（[rm_hub/\_\_init\_\_.py:L34-L52](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/rm_hub/__init__.py#L34-L52)）的实现：它用共享 `aiohttp.ClientSession`（连接池 64、超时 120s、指数退避重试 10 次）向 `--rm-url` POST `{prompt, response, label}`。
