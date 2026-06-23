# 测验内容生成流水线

## 1. 本讲目标

在上一篇 [u6-l1](u6-l1-quiz-app.md) 里，我们把 `etc/quiz-app/` 这个 Vue 测验应用拆解清楚了：它靠读取一份份 `lesson-N.json` 题库、按路由 `/quiz/:id` 把题目渲染出来。但有一个问题一直被悬置——**这些 JSON 题库是从哪儿来的？总不会是有人手写 24 个文件的吧？**

本讲就来补上这一环。读完本讲，你应当能够：

1. 看懂纯文本源文件 `questions-en.txt` 的题目书写格式（四种行首标记）。
2. 逐行讲清生成脚本 `qzmkjson.py` 是如何把这份文本解析、分组、再写出 24 份 `lesson-N.json` 的。
3. 理解生成产物如何被前端 `index.js` 聚合、再被 `Quiz.vue` / `Home.vue` 消费，从而打通「源文本 → 脚本 → JSON → 前端渲染」的完整闭环。
4. 动手新增一道题、跑一遍流水线，并亲眼看到一个**源码与前端之间的隐藏耦合**。

## 2. 前置知识

- **本讲承接 [u6-l1](u6-l1-quiz-app.md)**：你已经知道 quiz-app 是 Vue 2 单页应用，测验数据来自 `@/assets/translations`，且每套测验**硬编码只显示 3 道题**（`Quiz.vue` 里的 `nextQuestion < 3`）。本讲会再次用到这个结论。
- **JSON 结构**：一种键值对文本格式，`[ ]` 表示数组、`{ }` 表示对象。本讲的 `lesson-N.json` 就是嵌套了两层的「对象包数组」结构。
- **Python 基础**：能看懂 `for` 循环、字典、`open()` 读写文件即可。生成脚本只有 60 多行，没有任何框架。
- **前端模块导入**：知道 `import x from "./a.json"` 能把 JSON 当 JS 对象引入即可。

一句话直觉：**写题的人只想打字、不想碰 JSON；前端只想读结构化数据、不想解析文本。脚本就是中间那座桥。** 这种「人写易读的源格式 → 脚本 → 机器易用的结构化格式」是所有内容生成流水线的共同骨架（Markdown 转 HTML、YAML 转配置，都是同一套思路）。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲用途 |
| --- | --- | --- |
| `etc/quiz-src/questions-en.txt` | **源**：人类书写题目的纯文本 | 讲解四种行首标记的格式约定 |
| `etc/quiz-src/qzmkjson.py` | **转换器**：60 行 Python 脚本 | 逐行精读解析与生成逻辑 |
| `etc/quiz-src/template.json` | **模板**：每课 JSON 的骨架 | 讲解 `deepcopy` 复制机制 |
| `etc/quiz-app/src/assets/translations/en/lesson-1.json` | **产物**：生成出的题库示例 | 对照源文本，确认转换正确 |
| `etc/quiz-app/src/assets/translations/en/index.js` | **聚合**：脚本同时生成的导入表 | 讲解前端如何加载 24 份 JSON |
| `etc/quiz-app/src/assets/translations/index.js` | **多语言分发**：按 locale 选 JSON | 讲解 `en`/`es` 两套题库如何挂到 `messages` |
| `etc/quiz-app/src/components/Quiz.vue` | **消费端**：渲染测验的组件 | 回顾它如何用 `quiz.id` 取题 |
| `etc/quiz-app/src/views/Home.vue` | **消费端**：列出全部测验链接 | 讲解 `quiz.id` 如何变成 URL |

## 4. 核心概念与源码讲解

本讲按数据流向拆成三个最小模块：**题目源格式 → 生成脚本逻辑 → JSON 题库与前端对接**。

### 4.1 题目源格式：questions-en.txt

#### 4.1.1 概念说明

`questions-en.txt` 是整个测验体系的**唯一内容源头**（single source of truth）。它是一个纯文本文件，全课程 24 课、每课「课前 + 课后」两套测验、共 48 套题，全部用一种**行首标记法**（prefix notation）写在里面。

设计这种文本格式的出发点是：**让写题的人能用记事本写、用 diff 审阅、用 Git 跟踪改动**，完全不用关心 JSON 的引号、缩进、逗号。每一行的**第一个字符**就决定了这一行是什么角色。

#### 4.1.2 核心流程

整个文件只用到 **4 种行首**，外加空行做分隔：

| 行首 | 含义 | 示例 |
| --- | --- | --- |
| `Lesson` | 开启一套新测验，后面跟课程编号 + 标题 | `Lesson 1B Introduction to AI: Pre Quiz` |
| `* ` | 一道题目的题干（question） | `* A famous 19th century proto-computer engineer was` |
| `+ ` | 一个**正确**选项（correct answer） | `+ Charles Babbage` |
| `- ` | 一个**错误**选项（wrong answer） | `- Charles Darwin` |
| 空行 | 视觉分隔，解析时被跳过 | （空行） |

其中最关键的是 `Lesson` 行里的**课程编号**，它用一个数字加一个字母编码「哪一课」与「课前/课后」：

- `1B` = 第 1 课、**B**efore（课前测验）
- `1E` = 第 1 课、**E**nd（课后测验）
- 以此类推，`4B`/`4E`、`24B`/`24E`……

字母 `B`/`E` 会直接决定生成的 JSON 里这道题的 `id`（见 4.2）。

一个完整的「一套测验」由「一行 `Lesson` + 若干组（一行 `*` + 若干行 `+`/`-`）」组成。看真实文本：

```text
Lesson 1B Introduction to AI: Pre Quiz
* A famous 19th century proto-computer engineer was
- Charles Barkley
+ Charles Babbage
- Charles Darwin
* Weak AI is a system designed to solve many tasks
- True
+ False
```

#### 4.1.3 源码精读

源文件头部的「课前测验 1B」[questions-en.txt:1-5](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/quiz-src/questions-en.txt#L1-L5) —— 这五行就完整定义了一套测验的第一道单选题：`Lesson 1B ...` 声明测验，`* ...` 是题干，三个 `-`/`+` 是三个选项，其中 `+ Charles Babbage` 是正确答案。

紧接着的课后测验 [questions-en.txt:14-25](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/quiz-src/questions-en.txt#L14-L25) 用 `Lesson 1E ...` 开头，与课前测验隔一个空行——注意**两套测验之间靠空行分隔，但解析器其实只认 `Lesson` 行来切分测验、空行只是给人看的**（4.2 会看到脚本对空行是直接 `continue` 跳过的）。

全文件共 48 行 `Lesson` 开头，正好对应 24 课 × 2 套 = 48 套测验。

#### 4.1.4 代码实践

**目标**：用眼睛当解析器，确认你对格式的理解无误。

1. 打开 [questions-en.txt](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/quiz-src/questions-en.txt)。
2. 找到第 40 行的 `Lesson 2E ...`（符号 AI 的课后测验）。
3. 数一数它下面有几道题、每道题几个选项、正确选项分别是哪个。
4. **预期结果**：3 道题；前两题各 2 个选项（True/False 判断题），第三题 3 个选项。正确答案分别以 `+` 开头。
5. 如果某套测验里你数出**不是 3 道题**，记下来——这会直接关系到 4.3.4 里要揭示的前端契约。

#### 4.1.5 小练习与答案

**练习 1**：如果要新增一套「第 7 课的课前测验」，`Lesson` 行的编号应该写成什么？为什么？

> **答案**：`7B`。数字 `7` 是课号，字母 `B` 表示 Before（课前）。课后测验则是 `7E`。这套 B/E 编码会被脚本换算成 `id`（707/207 等等，见 4.2.2），从而让课前和课后测验拥有不同的 URL。

**练习 2**：下面这段文本里，正确答案是哪一句？
```text
* To minimize the function of weights, you can use gradient descent
+ true
- false
```

> **答案**：`true`。只有行首是 `+` 的选项才是 `isCorrect: true`。注意 `+`/`-` 后面有一个空格，解析时会被脚本用 `l[2:]` 切掉（见 4.2.3）。

### 4.2 生成脚本逻辑：qzmkjson.py

#### 4.2.1 概念说明

`qzmkjson.py` 是一个只有 62 行的 Python 脚本，干的事情可以用一句话概括：**读 `questions-en.txt` → 解析成内存里的嵌套字典 → 写成 24 份 `lesson-N.json` 加 1 份 `index.js`**。

它用到的核心编程思想有三个：

1. **状态机解析**（state machine）：脚本维护几个「上一次见到的对象」变量（`prev_q`、`prev_l`），每读一行就根据行首决定是「开始新对象」还是「往旧对象里塞东西」。这是处理无缩进纯文本的常用手法。
2. **模板 + 深拷贝**（template + deepcopy）：每课 JSON 都共享同一个外壳（标题、完成语、错误语），只有 `quizzes` 数组不同。脚本读一次模板、每课 `deepcopy` 一份再填内容，避免共享引用导致数据串台。
3. **按课号分组**（grouping）：源文本里 `1B` 和 `1E` 是两套独立的测验，但产物里它们要被**合并进同一个 `lesson-1.json`** 的 `quizzes` 数组。脚本靠 `int(k[:-1])`（去掉末尾字母取课号）来归并。

#### 4.2.2 核心流程

脚本整体分四步，伪代码如下：

```
# 第 0 步：id 编码 —— B/E 后缀换算成数字
def mk_id(s):           # "1B" -> 101, "1E" -> 201
    lesson_no = int(s[:-1])
    return lesson_no + (100 if s 末尾是 'B' else 200)

# 第 1 步：逐行状态机解析，得到 lessons 字典
#   key = 原始编号字符串（"1B", "1E", ...）
#   value = {id, title, quiz:[题目...]}
读入 questions-en.txt 的每一行 l:
    若 l 以 '+' 或 '-' 开头  -> 这是选项，塞进 prev_q['answerOptions']
    若 l 以 '*' 开头         -> 这是新题，把上一题收进 prev_l，再开新 prev_q
    若 l 以 'Lesson' 开头    -> 这是新测验，把上一套收进 lessons，再开新 prev_l
    若是空行                 -> 跳过
    否则                     -> 报错（格式异常）

# 第 2 步：按课号分组
#   lessons["1B"] 与 lessons["1E"] 合并进 lesson_content[1]
对 lessons 里每一条 (k, v):
    no = int(k[:-1])          # "1B" -> 1
    若 lesson_content[no] 不存在 -> 深拷贝模板建一份
    把 v 追加进 lesson_content[no][0]['quizzes']

# 第 3 步：写 24 份 lesson-N.json
对 lesson_content 里每一条 (no, content):
    json.dump 写到 translations/en/lesson-{no}.json

# 第 4 步：写 index.js（24 条 import + 一张映射表）
对每个课号 k:
    写 import x{k} from "./lesson-{k}.json";
写 const quiz = { 0: x1[0], 1: x2[0], ... }; export default quiz;
```

id 编码的数学含义可以写成：

\[
\text{id} = n + \begin{cases} 100 & \text{若后缀为 } B \\ 200 & \text{若后缀为 } E \end{cases}, \quad n \in \{1,\dots,24\}
\]

所以课前测验 id 落在 101–124，课后测验落在 201–224，两段不重叠——这正是课前/课后能拥有不同 URL（如 `/quiz/101` vs `/quiz/201`）的根本原因。

#### 4.2.3 源码精读

**id 编码函数** [qzmkjson.py:9-11](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/quiz-src/qzmkjson.py#L9-L11) —— `s[:-1]` 去掉末尾字母得到课号，再用三目表达式按 `B`/`E` 加 100 或 200。注释里的 `4B/4E` 是举例。

**两个运行前置条件**，脚本第 1–2 行写死了相对路径 [qzmkjson.py:1-2](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/quiz-src/qzmkjson.py#L1-L2)：输入是当前目录的 `questions-en.txt`，输出写到 `../quiz-app/src/assets/translations/en`。**这意味着脚本必须从 `etc/quiz-src/` 目录内运行**，否则两个路径都会失效。

**一个隐藏的依赖陷阱** [qzmkjson.py:7](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/quiz-src/qzmkjson.py#L7) —— `from matplotlib.cbook import ls_mapper` 这一行导入了 matplotlib，但 `ls_mapper` 在整个脚本里**从未被使用**。这是一段死代码（dead import），副作用是：**跑这个脚本的前提是你的环境里装了 matplotlib**，否则直接 `ImportError`。一个不画图的文本转换脚本却依赖绘图库，是这套代码遗留的一个小历史包袱。

**状态机解析主循环** [qzmkjson.py:23-44](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/quiz-src/qzmkjson.py#L23-L44) —— 这是脚本的核心。四个分支的判定顺序很关键：

- **选项分支**（26–27 行）：行首 `+`/`-` 的处理用 `l[2:]` 切掉「符号 + 一个空格」两字符拿到选项文本，用 `l.startswith('+')` 直接得到 `isCorrect` 布尔值，塞进当前题目 `prev_q`。
- **题目分支**（28–31 行）：遇到 `*` 说明上一题（`prev_q`）写完了，先把它收进当前测验 `prev_l['quiz']`，再开一道新的空题目。
- **测验分支**（32–39 行）：遇到 `Lesson` 说明上一套测验写完了，先把最后一题收尾、把上一套测验存进 `lessons`，再用 `mk_id` 算出数字 id、解析出标题，开一套新的空测验。注意 `prev_l_id = l[7:l.find(' ',7)]` 这行是在**切出编号子串**：跳过前 7 个字符（`Lesson ` 共 7 字符），取到下一个空格为止，正好拿到 `1B`/`1E` 这样的编号。
- **收尾**（43–44 行）：循环结束后，最后一套测验的最后一题还在「待存」状态，要补两次 append 才能落袋。

**按课号分组** [qzmkjson.py:46-51](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/quiz-src/qzmkjson.py#L46-L51) —— `no = int(k[:-1])` 把 `"1B"`、`"1E"` 都归约成 `1`，于是课前课后两套测验被 `append` 进**同一个** `lesson_content[1][0]['quizzes']`。这就是为什么 [lesson-1.json](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/quiz-app/src/assets/translations/en/lesson-1.json) 的 `quizzes` 数组里同时有 id=101（课前）和 id=201（课后）两套题。`deepcopy(doc)` 保证每课各拿一份模板副本、互不干扰。

**生成 index.js** [qzmkjson.py:53-58](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/quiz-src/qzmkjson.py#L53-L58) —— 这一程值得细看。它用 `enumerate` 给每课编号 `i`（从 0 开始），但写出来的对象键用的是 `i` 而非课号 `k`：

```python
t = ', '.join([ f"{i} : x{k}[0]" for i,k in enumerate(lesson_content.keys())]);
# 结果：{ 0 : x1[0], 1 : x2[0], 2 : x3[0], ... }
```

也就是说 `quiz[0]` 才是第 1 课、`quiz[1]` 是第 2 课——**键是 0 基下标，不是课号**。好在课号 1–24 连续，两者恰好错一位。这是个**潜在脆弱点**：如果哪天少了某课，0 基下标就会和课号对不上（4.3.2 会看到前端其实靠遍历而非按下标取值，所以暂时没暴露问题）。

#### 4.2.4 代码实践

**目标**：亲手跑一遍脚本，观察它如何把文本变成 JSON。

1. 确认你的 `ai4beg` 环境（或任意装了 matplotlib 的 Python 环境）可用。
2. **进入正确目录**（脚本写死了相对路径）：`cd etc/quiz-src`
3. 运行：`python qzmkjson.py`
4. 观察控制台输出：**正常情况下应该没有任何 `Error:` 打印**（脚本第 41 行只在遇到无法识别的行时才报错）。如果有 `Error:`，说明 `questions-en.txt` 里有格式不合规的行。
5. 用 `git status` 看产物目录 `etc/quiz-app/src/assets/translations/en/`：**如果你没有改源文件，产物应当与仓库里完全一致、git 无 diff**——这能验证脚本可复现地生成已提交的题库。
6. **待本地验证**：若你的环境没装 matplotlib，第 2 步会报 `ModuleNotFoundError: No module named 'matplotlib'`，这也印证了 4.2.3 提到的死依赖。可用 `pip install matplotlib` 后重试。

#### 4.2.5 小练习与答案

**练习 1**：脚本第 41 行的 `print(f"Error: {l}")` 在什么情况下会触发？如果触发，会不会导致生成的 JSON 出错？

> **答案**：当某行既不以 `+`/`-`/`*`/`Lesson` 开头、也不是空行时触发——比如某行题干漏写了 `*` 前缀。触发时脚本只是打印告警、**不抛异常也不中断**，那行文本会被直接丢弃，生成的 JSON 会比预期少一道题或一个选项。这也是为什么 4.2.4 第 4 步要求「跑完检查没有 Error」。

**练习 2**：如果把源文件里 `Lesson 1B` 的 `1B` 改成 `1A`（一个不存在的后缀），生成的 `lesson-1.json` 里课前测验的 `id` 会变成多少？

> **答案**：会变成 `1 + 200 = 201`，因为 `mk_id` 的逻辑是「只有 `B` 加 100，**其余全部**加 200」。任何不是 `B` 的后缀都会被当成课后测验。这是个不健壮的设计——它靠 `B`/`E` 的严格约定工作，不做校验。

### 4.3 JSON 题库与前端对接

#### 4.3.1 概念说明

脚本跑完，磁盘上多出两样东西：**24 份 `lesson-N.json`**（每份装一课的课前+课后测验）和**一份 `index.js`**（把 24 份 JSON 聚合成一个可导入的对象）。它们要被 Vue 前端消费，中间还隔着一层**多语言分发** `translations/index.js`。

本模块回答三个问题：

1. 单份 `lesson-N.json` 的结构长什么样、和模板什么关系？
2. `index.js` 怎么把 24 份 JSON 拼成前端要的对象？
3. 前端 `Quiz.vue` / `Home.vue` 又是怎么从这堆数据里**精准取到某一题**的？

#### 4.3.2 核心流程

整条消费链路如下：

```
questions-en.txt
      │  qzmkjson.py
      ▼
lesson-1.json ... lesson-24.json   ← 每份: [{ title, complete, error, quizzes:[课前, 课后] }]
      │  index.js（也是脚本生成的）
      ▼
{ 0: lesson1[0], 1: lesson2[0], ..., 23: lesson24[0] }   ← en 模块导出
      │  translations/index.js
      ▼
messages = { en: <上面的对象>, es: <西班牙语版> }
      │  import 到 Quiz.vue / Home.vue
      ▼
v-for 遍历 messages[locale] 的每个值 → 再遍历 quizzes → 用 quiz.id 匹配路由
```

关键点：

- **模板只读一次、每课各拷一份**：`template.json` 的 `quizzes` 数组初始为空，脚本 `deepcopy` 后往里 append 课前和课后两套，所以每份 `lesson-N.json` 的外壳（`title`/`complete`/`error`）完全一样。
- **前端不按下标、按 `id` 取题**：`Quiz.vue` 和 `Home.vue` 都用 `v-for` 把 24 课全遍历一遍，再用 `route == quiz.id` 或 `quiz/${quiz.id}` 精准命中。所以 4.2.3 提到的「0 基下标」其实不影响渲染——它只是个无人按下标访问的键。
- **`quiz.id` 就是 URL**：`Home.vue` 把每套测验生成 `<router-link :to="quiz/${quiz.id}">`，于是 `/quiz/101` 就是第 1 课课前测验。这正是 4.2.2 里 `mk_id` 那套 100/200 编码存在的全部意义。

#### 4.3.3 源码精读

**模板骨架** [template.json:1-9](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/quiz-src/template.json#L1-L9) —— 注意它整体被包在一个**数组** `[ { ... } ]` 里，`quizzes` 是空数组。这解释了为什么后续到处出现 `[0]`：`lesson_content[no][0]['quizzes']`、`index.js` 里的 `x1[0]`——都是在取这个数组的首个（也是唯一一个）元素。

**产物结构** [lesson-1.json:7-27](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/quiz-app/src/assets/translations/en/lesson-1.json#L7-L27) —— 对照 4.1.3 的源文本：`id: 101` 来自 `mk_id("1B")`，`title` 是 `Lesson 1B ` 之后的部分，`quiz` 数组里的每个对象对应源文件里一道 `*` 题，`answerOptions` 对应 `+`/`-` 行、`isCorrect` 对应符号。**文本里的每一行都能在 JSON 里找到精确的落点**，这就是可复现转换的检验标准。

**同一文件里的第二套测验** [lesson-1.json:60-62](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/quiz-app/src/assets/translations/en/lesson-1.json#L60-L62) —— `id: 201`（课后），与 101 同处一个 `quizzes` 数组，印证了 4.2.3 的「按课号合并」逻辑。

**聚合表** [index.js:1-24 与导出](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/quiz-app/src/assets/translations/en/index.js#L1-L27) —— 24 条 `import` + 末尾一行 `const quiz = { 0 : x1[0], ..., 23 : x24[0] }`。每个值都带 `[0]`，剥掉模板那层外层数组。注意键从 `0` 到 `23`，与课号 1–24 错一位。

**多语言分发** [translations/index.js:1-8](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/quiz-app/src/assets/translations/index.js#L1-L8) —— 把 `en` 和 `es` 两套题库挂到同一个 `messages` 对象上，键就是 locale 字符串。`es` 目录目前只有第 1 课，所以西班牙语版是不完整的——这是这套多语言机制的现实状态（详见 [u6-l4](u6-l4-translations-i18n.md)）。

**消费端 1：Home.vue 列出链接** [Home.vue:3-12](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/quiz-app/src/views/Home.vue#L3-L12) —— 外层 `v-for="q in questions[currLocale]"` 遍历当前语言的全部课（24 个），内层 `v-for="quiz in q.quizzes"` 遍历每课的课前+课后，`:to="quiz/${quiz.id}"` 把 `id` 直接拼进路由。这就是 `/quiz/101`、`/quiz/201` 这些 URL 的诞生地。

**消费端 2：Quiz.vue 命中并渲染** [Quiz.vue:3-6](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/quiz-app/src/components/Quiz.vue#L3-L6) —— 同样的双层遍历，但加了 `v-if="route == quiz.id"`，只有路由里的 `id`（来自 `this.$route.params.id`）匹配的那一套测验才会渲染。题目文本取自 `quiz.quiz[currentQuestion].questionText`，选项来自 `.answerOptions`。

**那个隐藏契约** [Quiz.vue:69-74](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/quiz-app/src/components/Quiz.vue#L69-L74) —— 注释赫然写着 `//always 3 questions per quiz`，`nextQuestion < 3` 决定答对后是进下一题还是标记完成。**这条硬编码把「每套测验 3 题」变成了源文件、脚本、前端三者的隐式契约**：源文件每套都写 3 题、脚本如实转换、前端只认 3 题。下一节的实践会让你亲手验证这个契约。

#### 4.3.4 代码实践

**目标**：完整跑通「改源 → 生成 → 前端显示」全链路，并验证那个 3 题契约。

1. 在 [questions-en.txt](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/quiz-src/questions-en.txt) 第 1 套测验（`Lesson 1B`）的末尾、空行之前，**仿照格式新增第 4 道题**：
   ```text
   * This course is offered by
   - Google
   + Microsoft
   - Apple
   ```
2. `cd etc/quiz-src && python qzmkjson.py` 重新生成。
3. 打开 `etc/quiz-app/src/assets/translations/en/lesson-1.json`，确认 id=101 的那套测验的 `quiz` 数组现在有 **4 道题**——证明脚本忠实转换了你的新增内容。
4. 按 [u6-l1](u6-l1-quiz-app.md) 的方法启动 quiz-app（`cd etc/quiz-app && npm install && npm run serve`），在浏览器打开首页，点进「Introduction to AI: Pre Quiz」（即 `/quiz/101`）。
5. **需要观察的现象**：依次答对前 3 道题后，页面**立即**显示「Congratulations, you completed the quiz!」，**第 4 道题根本不会出现**。
6. **预期结果 / 待本地验证**：这正是 [Quiz.vue:70](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/quiz-app/src/components/Quiz.vue#L70) 的 `nextQuestion < 3` 在起作用——脚本生成的 JSON 里确实有 4 题，但前端写死只取前 3 题。你亲眼看到了**源格式与前端之间这道隐藏耦合**。要让第 4 题生效，得同时改 `questions-en.txt`、重新生成、并把 `Quiz.vue` 的 `< 3` 改成对应题数——这正说明了「契约」之所以叫契约，是因为它跨了三个文件。
7. 实验完毕记得把源文件改动还原（`git checkout etc/quiz-src/questions-en.txt`），避免污染题库。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `index.js` 用 `x1[0]` 而不是 `x1`？这个 `[0]` 对应的是什么？

> **答案**：因为 `template.json` 的最外层是个数组 `[{ ... }]`（[template.json:1](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/quiz-src/template.json#L1)），`lesson-N.json` 继承了这个结构，所以 `x1` 是个长度为 1 的数组，`x1[0]` 才是真正的 `{title, complete, error, quizzes}` 对象。`[0]` 就是在剥掉这层冗余的外层数组。

**练习 2**：假设你想给西班牙语用户也提供第 2 课的测验，需要新增/修改哪些文件？脚本 `qzmkjson.py` 能直接帮上忙吗？

> **答案**：脚本**帮不上忙**——它写死了输出到 `translations/en`（[qzmkjson.py:2](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/etc/quiz-src/qzmkjson.py#L2)），且源文件只有 `questions-en.txt`。要加西语题，需要先有一份翻译好的 `questions-es.txt`，再改脚本（或复制一份）输出到 `translations/es/lesson-2.json`。目前仓库的 `es` 目录只有第 1 课，说明西语翻译尚未覆盖全课——这也是 [u6-l4 多语言翻译机制](u6-l4-translations-i18n.md) 要专门讨论的话题。

## 5. 综合实践

把本讲三个模块串起来，完成一次「**给课程加一道新测验并验证全链路**」的小工程：

1. **设计题目**（用 4.1 的格式）：为第 3 课「感知机」**新写一套课后测验** `Lesson 3E` 的内容（注意：源文件里其实已有 `3E`，为避免冲突，请你**替换**它现有的三道题为三道你自编的感知机题目，正确选项用 `+`、错误用 `-`）。题目可参考 [u2-l3 感知机讲义](u2-l3-perceptron.md) 的知识点（如线性可分、权重更新规则、XOR 反例）。
2. **生成**：`cd etc/quiz-src && python qzmkjson.py`，确认控制台无 `Error:`。
3. **核对产物**：打开 `lesson-3.json`，确认 id=203 的那套测验的 `quiz` 数组有 **恰好 3 道题**、每题 `answerOptions` 里**只有一个 `isCorrect: true`**——这是避免前端「答错」怪异行为的必要条件。
4. **验证 id 唯一性**：用 `grep '"id"' etc/quiz-app/src/assets/translations/en/lesson-*.json` 检查，确保 101–124 与 201–224 各不重复；若你自编题时误把编号写成 `3B` 重复，会导致两个测验抢同一个 URL。
5. **前端验证（待本地验证）**：启动 quiz-app，在首页找到你改的那套测验点进去，答对你设计的 3 道题，应看到完成语；再故意答错一题，应看到「Sorry, try again」。
6. **写一份流水线笔记**：用一段话总结「我改了 1 个文本文件的 N 行 → 脚本生成了 24 个 JSON + 1 个 JS → 前端无需改动就显示了我的新题」，并指出这个流程里**哪一步如果失败会让前端显示旧数据**（提示：前端拿的是构建时打包进 bundle 的 JSON，不是运行时读取，所以不重新构建前端就拿不到新 JSON）。

## 6. 本讲小结

- **`questions-en.txt` 是唯一内容源**：用 4 种行首（`Lesson`/`*`/`+`/`-`）+ 空行，把全课程 48 套测验写成一份人能直接读写的纯文本；`B`/`E` 后缀编码课前/课后。
- **`qzmkjson.py` 是一个 62 行状态机**：逐行解析文本、用 `mk_id` 把 `B`/`E` 换算成 100/200 段的数字 id、用 `deepcopy` 模板按课号分组、最后写出 24 份 `lesson-N.json` 和 1 份聚合用的 `index.js`。
- **脚本有两个隐藏陷阱**：写死相对路径（必须从 `etc/quiz-src/` 内运行）和一段无用的 `matplotlib` 导入（让纯文本脚本强依赖绘图库）。
- **`quiz.id` 是贯穿全链路的钥匙**：它在脚本里生成、在 `index.js` 里被聚合并带 `[0]` 剥壳、在 `Home.vue` 里拼成 URL、在 `Quiz.vue` 里匹配路由——100/200 编码让课前课后拥有不同 URL。
- **源格式与前端之间存在隐式契约**：每套测验固定 3 道题，由源文件写法、脚本如实转换、`Quiz.vue` 的 `nextQuestion < 3` 三方共同维持；4.3.4 的实践让你亲手验证了这道耦合。
- **这套流水线只管英文**：多语言要靠翻译文件 + 改输出目录，西语目前仅覆盖第 1 课，留给 [u6-l4](u6-l4-translations-i18n.md) 展开。

## 7. 下一步学习建议

本讲讲清了「题库怎么生成」，至此 u6 单元的前两篇（[u6-l1](u6-l1-quiz-app.md) 渲染端 + 本讲生成端）已经把测验子系统的前后端闭环补齐。建议接着读：

1. **[u6-l3 Docsify 文档站点](u6-l3-docsify-site.md)**：从测验应用跳出来，看仓库根目录的 `index.html` 如何用 Docsify 把全部 Markdown 讲义变成一个可在线浏览的站点——同样是「文本源 → 工具 → 浏览产物」的流水线，但用的是纯前端渲染。
2. **[u6-l4 多语言翻译机制](u6-l4-translations-i18n.md)**：本讲反复提到的「es 只有第 1 课」会如何被 `co-op-translator` 自动化翻译流程改善，以及稀疏克隆跳过 `translations/` 大目录的技巧。
3. **延伸阅读**：如果你对本讲的「源格式 → 脚本 → 结构化产物」模式感兴趣，可对比 `etc/quiz-app/` 的 `package.json` 脚本字段，看前端侧如何用 `npm run build` 把 Vue 源码「编译」成静态站点——本质是同一类「转换流水线」思想在不同层的复现。
