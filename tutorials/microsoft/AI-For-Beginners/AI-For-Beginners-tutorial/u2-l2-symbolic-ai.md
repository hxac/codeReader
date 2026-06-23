# 符号 AI：知识表示与专家系统

## 1. 本讲目标

上一讲（u2-l1）我们建立了 AI 的「范式坐标系」：自上而下的**符号主义**（GOFAI）与自下而上的**连接主义**。本讲是全课唯一一次正面落地符号主义的课程，对应课程的 `lessons/2-Symbolic`。学完本讲你应当能够：

1. 说清 **数据 / 信息 / 知识 / 智慧**（DIKW）四个层次的差别，理解为什么「把知识写进计算机」本身就是一个难题。
2. 看懂 **专家系统（Expert System）** 的三件套——工作记忆、知识库、推理引擎——并能区分 **前向推理（forward inference）** 与 **反向推理（backward inference）**。
3. 理解 **本体（Ontology）**、**三元组（triplet）** 与 **语义网（Semantic Web）** 是如何用一张「图」来组织知识的，并亲手跑通 `FamilyOntology.ipynb` 的自动推理。
4. 认识 **概念图（Concept Graph）** 这种从文本中「挖掘」出来的 `is-a` 层级知识。

本讲承接 u2-l1：上一讲讲的是「符号 AI 为什么衰落」，本讲讲的是「符号 AI 在它擅长的事情上到底是怎么工作的」，从而为后续连接主义课程（感知机、神经网络）提供一个对照基线。

## 2. 前置知识

本讲几乎不需要数学，但需要几个概念铺垫：

- **范式（paradigm）**：解决一类问题的总体思路。符号 AI 的范式是「把人类专家脑子里的规则抽取出来写成机器可执行的形式」。
- **规则 / 产生式规则（production rule）**：形如 `IF 条件 THEN 结论` 的语句。例如 `IF 发高烧 OR C 反应蛋白高 THEN 有炎症`。这是本讲最核心的「知识单元」。
- **三元组（triplet）**：用 `(主语, 谓语, 宾语)` 三个元素表达一条事实，例如 `(Python, 发明者, Guido)`。它是语义网里知识的最小颗粒。
- **图（graph）**：节点 + 边的结构。本讲里节点是「概念 / 实体」，边是「关系」。
- **闭包（closure）**：在一组规则下，把所有「能推导出来但还没显式写出」的事实全部补全后的完整集合。这是本讲 `FamilyOntology.ipynb` 的核心动作。

> 如果你已经做过 u1-l4 的 `examples` 练习，可以把本讲理解成它的「反面」：那里是**让机器从数据里自己学权重**（连接主义），本讲是**人类直接把规则喂给机器**（符号主义）。

## 3. 本讲源码地图

本讲涉及的关键文件都在 `lessons/2-Symbolic/` 目录下：

| 文件 | 作用 | 本讲用法 |
|------|------|----------|
| [README.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/2-Symbolic/README.md) | 课程讲义正文，系统讲解知识表示、专家系统、本体、概念图 | 概念的主要来源 |
| [Animals.ipynb](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/2-Symbolic/Animals.ipynb) | 用纯 Python 实现一个动物识别专家系统，含反向推理（自研）与前向推理（Experta 库） | 专家系统模块的精读对象 |
| [FamilyOntology.ipynb](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/2-Symbolic/FamilyOntology.ipynb) | 把罗马诺夫王朝家谱（GEDCOM）+ 亲属关系本体（OWL）拼成一张图，做闭包推理并用 SPARQL 查询 | 本体 / 概念图模块的精读对象 |
| [data/onto.ttl](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/2-Symbolic/data/onto.ttl) | 用 Turtle 语法写的亲属关系本体，定义了叔叔、姑姑、堂表亲等关系 | 实践任务的修改对象 |
| [data/tsars.ged](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/2-Symbolic/data/tsars.ged) | 罗曼诺夫家族的 GEDCOM 家谱数据 | Notebook 的事实来源 |

> 提醒：本讲所有 Notebook 都在 u1-l3 搭建的 `ai4beg` 环境里运行。Animals/Family 两个 Notebook 都会在运行时用 `pip install` 现装 `experta`、`python-gedcom`、`rdflib`、`owlrl` 等依赖，首次跑需要联网。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**① 知识与本体**、**② 专家系统规则推理**、**③ 概念图**。

### 4.1 知识与本体

#### 4.1.1 概念说明

符号 AI 的第一个大问题就是 **知识表示（Knowledge Representation）**：怎么把人脑子里的知识，用计算机能用的形式（也就是数据）写下来。

这里要先区分一个容易被忽视的差别——**数据 ≠ 知识**。README 里点明：

> 书里装的是「数据」，只有当你读完书、把它整合进自己的世界模型后，它才变成你脑子里的「知识」。

README 用 [DIKW 金字塔](https://en.wikipedia.org/wiki/DIKW_pyramid) 把这件事讲成四个层次，由低到高是：

- **数据（Data）**：写在物理介质上的符号，独立于人，可传递。比如纸上的字。
- **信息（Information）**：人脑对数据的解读。比如听到「电脑」这个词，你脑子里有概念。
- **知识（Knowledge）**：信息被编织进你的世界模型，形成一张相互关联的概念网。
- **智慧（Wisdom）**：关于「何时、如何使用知识」的元知识。

知识表示，就是要在计算机里**重建那张概念网**。README 给出了一张「表示能力谱」：最左端是算法（计算机好用但不灵活），最右端是自然语言（最强大但无法自动推理），中间是一系列折中方案。

那么「本体（Ontology）」是什么？README 的定义是：

> 本体（Ontology）是对某个问题领域的一种**显式的、形式化的规约（explicit specification）**。

通俗讲，本体就是「把一个领域里的概念、概念之间的层级和关系，用一套有严格语义的规则写下来」。最简单的本体就是一棵对象的层级树（比如「金丝雀 是一种 鸟」）；复杂一点的本体还会带上可推理的规则。语义网（Semantic Web）就是想把整个互联网的资源都用这种本体标注起来，让机器能做精确查询。

#### 4.1.2 核心流程

README 把计算机里的知识表示方法分成几大类，本讲重点关注其中三种（它们正是后续源码里出现的）：

1. **网络式表示 → 语义网络**：把脑子里的概念网直接画成一张图。
2. **对象-属性-值三元组（OAV）**：图可以用「节点 + 边」表示，于是每条边就能写成一条三元组。例如关于 Python 的知识：

   | Object | Attribute | Value |
   |--------|-----------|-------|
   | Python | is | Untyped-Language |
   | Python | invented-by | Guido van Rossum |
   | Python | block-syntax | indentation |

3. **层次式表示 → 框架（Frame）**：每个对象/类是一个「框架」，框架里有若干「槽（slot）」，槽可以有默认值、取值约束等，框架之间形成层级（很像面向对象里的类继承）。
4. **过程式表示 → 产生式规则**：`IF ... THEN ...`，这正是专家系统的核心，详见 4.2。
5. **逻辑式表示**：谓词逻辑、描述逻辑（Description Logic，语义网的理论基础）。

本体 + 三元组的「推理流程」可以概括为：

```text
原始事实（三元组集合，例如 GEDCOM 解析出的 isMotherOf / isFatherOf）
        │
        ▼
   装进一张 RDF 图
        │
        ▼
用本体里的规则（propertyChainAxiom 等）做「闭包扩展」
   —— 把所有能推导出的新三元组都补进去 ——
        │
        ▼
得到完整的知识图（闭包）
        │
        ▼
用 SPARQL 查询（例如「列出所有叔叔」）
```

其中「闭包扩展」是关键：机器并不是逐条去套规则，而是把本体规则当作「等式」，不断把隐含的三元组显式化，直到不再产生新事实为止。

#### 4.1.3 源码精读

**① DIKW 与「数据≠知识」的定义** 出自 README 的知识表示一节：

[README.md:L16-L27](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/2-Symbolic/README.md#L16-L27) —— 给出 Knowledge 的定义，并依次列出 Data / Information / Knowledge / Wisdom 四层。这段是本讲概念的地基。

**② 知识表示谱 + 分类**：

[README.md:L33-L42](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/2-Symbolic/README.md#L33-L42) —— 左端「算法式」最刻板、右端「自然语言」最强但不可自动推理，知识表示就是在两端之间找折中。

[README.md:L44-L82](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/2-Symbolic/README.md#L44-L82) —— 把表示方法分成网络式（语义网络 + OAV 三元组）、层次式（Frame）、过程式（产生式规则）、逻辑式四类。注意第 50–57 行就是上面那张 Python 三元组表。

**③ 本体与语义网**：

[README.md:L157-L165](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/2-Symbolic/README.md#L157-L165) —— 讲语义网依赖描述逻辑、URI 全局命名、RDF/RDFS/OWL 语言族；第 165 行给出本体的定义。这是 4.3 节 `FamilyOntology.ipynb` 的理论铺垫。

**④ 一个真实本体的样子** 在 `data/onto.ttl` 里。它是用 Turtle 语法写的亲属关系本体，每条关系都是一个 `owl:ObjectProperty`，并通过 `owl:propertyChainAxiom`（属性链）定义「复合关系」。比如「叔叔 = 父母的兄弟」：

[data/onto.ttl:L193-L196](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/2-Symbolic/data/onto.ttl#L193-L196) —— `isUncleOf` 的定义：`propertyChainAxiom ( isBrotherOf isParentOf )`，读作「X 是 Y 的叔叔，当且仅当存在 Z，X 是 Z 的兄弟、Z 是 Y 的父母」。这正是 4.1.2 里说的「用规则把隐含关系推导出来」。

[data/onto.ttl:L209-L212](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/2-Symbolic/data/onto.ttl#L209-L212) —— `isAuntOf`（姑姑/阿姨）= `isSisterOf ∘ isParentOf`，结构与 `isUncleOf` 完全对称，只是把 `isBrotherOf` 换成 `isSisterOf`、domain 换成 `Woman`。本讲实践任务会照着这个模板新增关系。

#### 4.1.4 代码实践（源码阅读型）

**目标**：用肉眼读懂 `onto.ttl` 里「复合关系 = 属性链」这一种写法，为 4.3 和第 5 节的动手任务做准备。

**步骤**：

1. 打开 [data/onto.ttl](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/2-Symbolic/data/onto.ttl)，定位到第 193–220 行。
2. 对比三组定义：`isUncleOf`、`isGreatUncleOf`（叔祖父）、`isAuntOf`。
3. 把每条 `propertyChainAxiom` 翻译成一句中文。

**需要观察的现象**：

- `isUncleOf` 用的是 `( isBrotherOf isParentOf )`——两段链。
- `isGreatUncleOf` 用的是 `( isBrotherOf isGrandParentOf )`——只是把第二段从「父母」升级成「祖父母」。
- `isFirstCousinOf`（堂表亲）用的是三段链 `( hasParent isSiblingOf isParentOf )`——「我的父母 的 兄弟姐妹 的 孩子」。

**预期结果**：你能发现一个模式——**只要换链条上的某一段，就能「批量生产」新的亲属关系**。这正是第 5 节综合实践要利用的性质。如果不确定语义，标注「待本地验证」后到 `FamilyOntology.ipynb` 里用 SPARQL 跑一遍确认。

#### 4.1.5 小练习与答案

**练习 1**：用一句话解释「数据」和「知识」的差别。

> **参考答案**：数据是写在介质上、可独立传递的符号；知识是这些数据被学习者主动整合进自己世界模型后形成的、相互关联的概念网。书里是数据，读完书存在你脑子里的才是知识。

**练习 2**：README 说知识表示是一条「谱」，最左端和最右端分别是什么？为什么两端都不理想？

> **参考答案**：左端是「算法式」表示——计算机好用但不灵活，因为人脑里的知识往往不是算法；右端是「自然语言」——表达力最强但无法被机器自动推理。好的知识表示要在「机器可计算」与「表达力」之间折中。

**练习 3**：`isUncleOf` 的属性链是 `( isBrotherOf isParentOf )`。请按同样写法，写出「侄子（姐妹或兄弟的儿子）」的属性链思路（提示：先想侄子是谁的儿子）。

> **参考答案**：「X 是 Y 的侄子」可拆成「X 是 Z 的儿子」且「Z 是 Y 的兄弟姐妹」，所以属性链可写成 `( isSonOf isSiblingOf )`。这正是第 5 节要新增的关系（注意需 `domain = Man`）。

---

### 4.2 专家系统规则推理

#### 4.2.1 概念说明

符号 AI 的第二个大问题是 **推理（Reasoning）**：有了写下来的知识，怎么让它自动得出结论？早期最成功的形态就是 **专家系统（Expert System）**——在某个狭窄领域里扮演人类专家的计算机系统。

README 把专家系统类比成人的推理系统（人脑有短期记忆和长期记忆），并拆成三个组件：

- **问题记忆（Problem Memory）/ 工作记忆**：当前这道题里已知的事实（比如病人的体温、血压）。也叫**静态知识**，因为它是一次咨询的「快照」。
- **知识库（Knowledge Base）**：从专家那里抽取出来的、跨咨询复用的规则。也叫**动态知识**，因为它驱动状态从一个跳到另一个。
- **推理引擎（Inference Engine）**：调度整个推理过程，决定下一步用哪条规则、必要时向用户提问。

> 关键设计：**知识与推理分离**。理想情况下，领域专家不必懂推理引擎的细节，只要会写规则即可。这也是为什么后面 `Animals.ipynb` 要专门搞一套「规则语法」。

知识库里的规则常用 **AND-OR 树** 来画：一棵树，叶子是可观测的事实，内部节点用 AND（必须都满足）/ OR（满足其一即可）组合，根节点是结论。例如「食肉动物」= 吃肉 OR（利齿 AND 利爪 AND 双眼前视）。

#### 4.2.2 核心流程

推理引擎有两种驱动方式，本讲两个 Notebook 各演示一种：

**前向推理（forward inference / 数据驱动）**——从已知事实出发，往结论推。README 给出循环：

1. 若目标属性已在工作记忆里 → 停，给出结果。
2. 找出所有「条件当前已满足」的规则，得到一个 **冲突集（conflict set）**。
3. **冲突消解（conflict resolution）**：从冲突集里挑一条来执行（策略如「第一条适用的」「更具体的」「随机」）。
4. 执行该规则，把新结论塞进工作记忆。
5. 回到第 1 步。

适合「一次性拿到大量观测数据」的场景，比如把一批检验结果喂进去自动诊断。

**反向推理（backward inference / 目标驱动）**——从一个 **目标**（想求的属性）出发，倒着找证据。README 给出递归流程：

1. 选出所有「右端能给目标赋值」的规则 → 冲突集。
2. 若没有规则能给该属性赋值，或规则要求向用户提问 → 直接问用户；否则：
3. 用冲突消解策略挑一条规则作为「假设」去证明。
4. 对该规则左端的每个属性，递归地把它们当新目标去证明。
5. 一旦某条路走不通 → 回退，换第 3 步的另一条规则。

适合「没必要一开始就做完全部检查」的场景，比如医生不会先把所有化验做完再诊断，而是按需开单。它的好处是**只问该问的问题**。

数学上，这两种推理可以看成在同一组规则上的两种搜索方向：前向是「从事实做广度扩展」，反向是「从目标做回溯」。两者最终能推出的结论集合（在完备的规则集下）是等价的，区别在于**提问的顺序和效率**。

#### 4.2.3 源码精读

`Animals.ipynb` 是本模块的主角。它先定义一套「规则描述语言」（几个 Python 类当关键词），再分别实现反向推理（自研）和前向推理（用 Experta 库）。

**① 规则语言的「语法糖」**（Notebook 第 2 个代码 cell，原始文件第 38–62 行）：

[Animals.ipynb:L38-L62](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/2-Symbolic/Animals.ipynb#L38-L62) —— 定义 `Ask`（向用户提问，带候选答案）、`If`（一条规则）、`AND`/`OR`（树的分支），它们都继承自 `Content`，只为把规则的内部结构存下来。这就是「知识与推理分离」里的「知识表示语言」。

**② 知识库本身**（第 4 个代码 cell，第 80–94 行）：

[Animals.ipynb:L80-L94](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/2-Symbolic/Animals.ipynb#L80-L94) —— 一个大字典 `rules`，把「动作（要插入的新事实）」映射到「条件（AND-OR 表达式）」。注意 `carnivor` 那条：`If(OR([AND(['sharp teeth','claws','forward-looking eyes']),'eats meat']))`，正是 README 那张 AND-OR 树的逐字翻译。`default` 和 `color`/`pattern` 是 `Ask`，意味着这些属性「问用户」。

**③ 反向推理引擎**（第 6 个代码 cell，第 119–173 行）：

[Animals.ipynb:L119-L173](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/2-Symbolic/Animals.ipynb#L119-L173) —— `KnowledgeBase` 类。两个核心方法：
- `get(name)`（第 124 行起）：求某属性的值。先查工作记忆 `self.memory`；没有就遍历规则，把规则右端匹配上 `name` 的当作待证明目标，调 `eval`；若规则也没有，则落到 `default`（即 `Ask`，问用户）。这正是 4.2.2 里「反向推理」第 1–2 步。
- `eval(expr, field)`（第 147 行起）：递归求值一棵 AND-OR 表达式。遇到 `Ask` 就提问；遇到 `If` 就剥开看内部；遇到 `AND`/列表就「全 y 才 y」；遇到 `OR` 就「任一 y 即 y」；遇到字符串就当成子目标递归调用 `get`。这就是反向推理的递归展开。

调用 `kb.get('animal')`（第 8 个代码 cell，第 225 行）后，引擎会从「动物是什么」这个目标倒推，按需逐个问你「有没有毛发」「什么颜色」，最终例如推出 `'giraffe'`。

**④ 前向推理：Experta 库**（第 13、15 个代码 cell，第 304–432 行）：

[Animals.ipynb:L304-L333](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/2-Symbolic/Animals.ipynb#L304-L333) —— `Animals(KnowledgeEngine)` 类。每条规则是一个被 `@Rule(...)` 装饰的方法，装饰器里写「触发条件」（如 `@Rule(Fact('mammal'), Fact('carnivor'), Fact(color='red-brown'), Fact(pattern='dark stripes'))`），方法体里用 `self.declare(...)` 往工作记忆加新事实。

[Animals.ipynb:L427-L432](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/2-Symbolic/Animals.ipynb#L427-L432) —— 先 `ex1.reset()` 清空、`ex1.factz([...])` 喂入一批初始事实（红棕色、深条纹、利齿……），再 `ex1.run()` 触发前向推理。运行结果会打印 `Animal is tiger`，并且 `ex1.facts` 里能看到从初始事实自动派生出的 `Fact('mammal')`、`Fact('carnivor')`、`Fact(animal='tiger')` 等新事实——这正是前向推理「事实驱动、不断派生新事实」的过程。README 提到 Experta 内部用 **Rete 算法** 做高效的规则匹配，避免每步都全表扫描。

#### 4.2.4 代码实践

**目标**：在 `ai4beg` 环境里跑通 `Animals.ipynb` 的反向推理，亲手当一次「被提问的用户」，体会反向推理「按需提问」的特点。

**步骤**：

1. 激活环境并启动 Jupyter（参考 u1-l3），打开 `lessons/2-Symbolic/Animals.ipynb`，选 `ai4beg` 内核。
2. 从头运行到第 8 个代码 cell（`kb.get('animal')`，原始文件第 225 行）。
3. 程序会逐行提问。请**故意按一只「长颈鹿（giraffe）」的特征**回答：毛发 `y`、利齿 `y`、利爪 `y`、双眼前视 `y`、颜色选 `0`（red-brown）、有蹄 `y`、长脖 `y`、长腿 `y`、花纹选 `1`（dark spots）。
4. 观察输出最后返回的字符串。
5. 再跑一次，这次**改成深条纹**（花纹选 `0`），观察推理路径与结果有何不同。

**需要观察的现象**：

- 反向推理不会一上来问你所有问题，而是先证明「是不是哺乳动物」「是不是食肉动物」，再往下走。
- 当一条路走不通（比如不满足食肉动物），它会去问别的分支，而不是直接失败。
- 同样的颜色问题只会被问一次——因为答案被存进了 `self.memory`（见 `get` 方法第 138 行附近），这就是「工作记忆」复用事实的效果。

**预期结果**：第一次返回 `'giraffe'`；若改成深条纹且具备有蹄等条件，应推导出 `'zebra'`。若你故意全答 `n`，最终会落到无法判定（取决于 `default` 行为）。若网络受限装不上 `experta`，反向推理部分（前 8 个 cell）不依赖它，可单独完成；前向推理部分标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：在 `Animals.ipynb` 的反向推理里，`color` 是 `Ask`，而 `carnivor` 是 `If(...)`。这两者在 `get` 方法里走的路径有什么不同？

> **参考答案**：`color` 没有规则可推导（不是任何 `If` 的左端目标），所以 `get('color')` 在遍历规则后落不到匹配项，最终走 `default` 分支调用 `Ask.ask()` 直接问用户；`carnivor` 是某条 `If` 的动作，`get('carnivor')` 会找到对应规则，调 `eval` 去证明其 AND-OR 条件。前者「问」，后者「推」。

**练习 2**：前向推理为什么要做「冲突消解」？如果同时有多条规则的条件都满足了会怎样？

> **参考答案**：一个工作记忆状态下可能同时满足多条规则（冲突集）。冲突消解就是从中挑一条先执行，避免歧义。策略可以是「知识库里第一条适用的」「条件更具体（左端更长）的」「随机」等。不消解的话，要么随机乱跑，要么得有明确的优先级，否则结论可能不稳定。

**练习 3**：医生诊断时通常不会一开始就把所有化验做完，而是按需开单。这种场景更适合前向还是反向推理？为什么？

> **参考答案**：更适合反向推理。反向推理由「目标（确诊某病）」驱动，递归地去证明所需证据，证据不足时才向用户（医生/化验）提问，因此只问该问的问题、节省成本。前向推理要把大量事实先备齐再推，恰好相反。

---

### 4.3 概念图

#### 4.3.1 概念说明

第三个最小模块是 **概念图（Concept Graph）**。它有两种含义，本讲都涉及：

1. **狭义：Microsoft Concept Graph**。README 介绍，这是微软研究院从**非结构化文本**里「挖掘」出来的一张大图，把实体用 `is-a`（是一个）继承关系串起来。它能回答「Microsoft 是什么？」这类问题——答案是「以 0.87 概率是一家公司、以 0.75 概率是一个品牌」。注意它和手工本体不同：**它是挖出来的，不是专家一条条写的**，所以每条边还带概率。
2. **广义：作为知识表示的「概念图 / 语义网络」**。4.1 节的语义网就是一种概念图——节点是概念，边是关系。`FamilyOntology.ipynb` 构建的就是一张「家族概念图」。

本节聚焦后者在代码里是如何「建图 → 推理 → 查询」的，这是符号 AI 落地最有代表性的一套流程。

#### 4.3.2 核心流程

`FamilyOntology.ipynb` 的整体流程是「把两个来源拼成一张可推理的图」：

```text
GEDCOM 家谱 (tsars.ged)              亲属关系本体 (onto.ttl)
   原始事实源                            规则源（OWL/Turtle）
        │                                      │
        ▼                                      │
   python-gedcom 解析出个人/家庭                │
        │                                      │
        ▼                                      ▼
   转写成三元组 (isMotherOf/isFatherOf     本体定义的复合关系规则
     /isBrotherOf/isSisterOf …)           (propertyChainAxiom)
        │                                      │
        └──────────────► 合并成 onto.ttl ◄──────┘
                              │
                              ▼
                  rdflib 载入为 RDF 图（初始三元组数 N₁）
                              │
                              ▼
            owlrl 做 DeductiveClosure（闭包扩展）
                  —— 三元组数从 N₁ 暴涨到 N₂ ——
                              │
                              ▼
                  SPARQL 查询（如「列出所有叔叔」）
```

关键的两个数字来自 Notebook：载入后图里有 **669** 条三元组，做完闭包后膨胀到 **4246** 条——多出来的 ~3500 条全是「能由规则推出、但原文没写」的隐含关系。这就是符号推理的威力：你只写了少量原子事实和规则，机器替你把整张关系网补全。

#### 4.3.3 源码精读

**① 解析 GEDCOM，把家谱转成三元组**（Notebook 第 12 个代码 cell，第 289–340 行）：

[FamilyOntology.ipynb:L289-L297](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/2-Symbolic/FamilyOntology.ipynb#L289-L297) —— `term2id` 把 GEDCOM 指针（如 `@0@`）转成稳定 URI 片段 `i0`；`out = open("onto.ttl","a")` 以**追加**方式把生成的事实拼到本体文件末尾。随后遍历所有个人/家庭，按性别选用 `isMotherOf/isFatherOf` 和 `isBrotherOf/isSisterOf` 谓词，把「某人是某些人的父母/兄弟姐妹」写成三元组。这一步是把「数据（GEDCOM）」转成「知识（三元组）」的典型动作。

**② 载入图 + 闭包扩展**（Notebook 第 17、19 个代码 cell，第 455–489 行）：

[FamilyOntology.ipynb:L455-L464](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/2-Symbolic/FamilyOntology.ipynb#L455-L464) —— `rdflib.Graph()` 载入 `onto.ttl`，打印 `Triplets found: 669`。这是「原始 + 规则」合并后、但尚未推理的三元组数。

[FamilyOntology.ipynb:L487-L489](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/2-Symbolic/FamilyOntology.ipynb#L487-L489) —— `DeductiveClosure(OWLRL_Extension).expand(g)` 执行闭包扩展，之后三元组数变为 `4246`。这一行是整张图「变聪明」的关键：它依据本体里的属性链等规则，把所有可推出的三元组显式化。

**③ SPARQL 查询「所有叔叔」**（Notebook 第 21 个代码 cell，第 515–527 行）：

[FamilyOntology.ipynb:L515-L527](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/2-Symbolic/FamilyOntology.ipynb#L515-L527) —— 一段 SPARQL：`?a fhkb:isUncleOf ?b` 配合 `rdfs:label` 取人名，遍历打印「X is uncle of Y」。注意：`isUncleOf` 在原始 GEDCOM 和 onto.ttl 的「事实」部分根本不存在，它完全是由 `propertyChainAxiom ( isBrotherOf isParentOf )` 在闭包阶段**推导**出来的。这正好演示了「知识 = 显式事实 + 规则推理」。

**④ Microsoft Concept Graph（狭义概念图）**：

[README.md:L213-L221](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/2-Symbolic/README.md#L213-L221) —— 介绍 Microsoft Concept Graph：从非结构化文本挖掘出的 `is-a` 实体层级，每个归类带概率（如「Microsoft → 公司 0.87、品牌 0.75」），可经 REST API 或大文本文件获取。这和 `FamilyOntology` 的手工本体形成对照：**前者是「挖」出来的概率图，后者是「写」下来的确定图**。配套的 `MSConceptGraph.ipynb` 演示用它给新闻分类（本讲不展开）。

#### 4.3.4 代码实践（源码阅读型 + 轻量验证）

**目标**：复用 Notebook 已有的推理结果，把查询从「叔叔」换成另一个**已定义但 Notebook 没查过**的关系，体会「同一次闭包，换个查询就能发现新连接」。

**步骤**：

1. 跑通 `FamilyOntology.ipynb` 到第 21 个代码 cell（查询 `isUncleOf`），确认能看到叔叔列表。
2. 在该 cell 下方**新建一个 cell**，把 SPARQL 里的 `fhkb:isUncleOf` 改成 `fhkb:isFirstCousinOf`（堂表亲，定义见 [data/onto.ttl:L225-L228](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/2-Symbolic/data/onto.ttl#L225-L228)），输出文案改成「X is cousin of Y」。
3. 再试一个 `fhkb:isAncestorOf`（祖先，[data/onto.ttl:L175-L176](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/2-Symbolic/data/onto.ttl#L175-L176)），注意它可能产生很多行（递归祖先）。

**需要观察的现象**：

- 你**没有重跑闭包**，只是换了查询谓词，却能查出全新的关系——因为这些关系早在 `expand(g)` 那一步就被推导进图里了。
- `isFirstCousinOf` 是 `SymmetricProperty`，所以「A 是 B 的堂表亲」和「B 是 A 的堂表亲」可能都出现。
- `isAncestorOf` 因为是传递性的（TransitiveProperty via `hasAncestor`），结果行数会远多于 `isUncleOf`。

**预期结果**：能列出若干堂表亲对/祖先对，这些都是原始 Notebook 没展示过的「新连接」。若某谓词查不出结果，说明该关系在当前家谱里确实无实例，属正常。若 `owlrl` 装不上，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么三元组数能从 669 涨到 4246？多出来的那些三元组是从哪儿来的？

> **参考答案**：`DeductiveClosure(OWLRL_Extension).expand(g)` 依据本体里的规则（尤其是 `owl:propertyChainAxiom` 定义的复合关系，以及传递、逆、对称属性等）做闭包扩展，把所有「逻辑上成立但原文没写」的三元组显式补全。多出来的 ~3500 条都是这样推导出的隐含关系（如所有叔叔、姑姑、堂表亲、祖先对）。

**练习 2**：Microsoft Concept Graph 和 `FamilyOntology` 里的本体，最本质的区别是什么？

> **参考答案**：来源与确定性不同。FamilyOntology 的本体是**人工编写**的确定规则（`is-a` 和属性链都有严格语义，结论确定）；Microsoft Concept Graph 是**从文本里挖掘**出来的，每条 `is-a` 边带概率（如 0.87），是统计性的、可能出错的。前者精度高但成本高、覆盖窄；后者覆盖广但需要容忍噪声。

**练习 3**：在 SPARQL 查询里，为什么我们要同时匹配 `?a rdfs:label ?aname`？只用指针 `?a` 会怎样？

> **参考答案**：`?a` 是形如 `fhkb:i0` 的内部 URI 片段，对人没有意义；`rdfs:label` 存的是可读人名（如「Mihail Fedorovich Romanov」，见 Notebook 第 12 个 cell 写入的 `rdfs:label`）。匹配 label 只是为了把结果打印成人能看懂的名字。不用 label 程序照样能跑，只是输出会是一堆 `i0/i17` 编号。

---

## 5. 综合实践

**任务**：在 `FamilyOntology.ipynb` 基础上**新增一条亲属关系规则**，并**查询出一个原本查不到的亲属关系**。本任务把「知识表示（写本体）→ 推理（闭包）→ 查询（SPARQL）」三个最小模块串起来。

我们新增 **「侄子 / 外甥（isNephewOf）」** 关系。经查 `data/onto.ttl`，`nephew/niece/in-law` 等关系**并不存在**（可对照 4.1.3 里 `isUncleOf` 的写法），所以这是真正的新增。利用练习 4.1.5-3 的结论：「侄子 = 兄弟姐妹的儿子」，写成属性链就是 `( isSonOf isSiblingOf )`。

**步骤**：

1. **复制一份可改的本体文件**。Notebook 第 12 个 cell 是 `!cp data/onto.ttl .` 把本体拷到当前目录再追加事实。请**不要改仓库里的 `data/onto.ttl` 原文件**（那是源码）。改为在工作目录里编辑拷贝出来的 `onto.ttl`，或另存一份 `onto_extra.ttl` 用于实验。

2. **在规则区追加新属性**（照搬 `isUncleOf` 的模板，[data/onto.ttl:L193-L196](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/2-Symbolic/data/onto.ttl#L193-L196)），在 `isUncleOf` 定义后面加入：

   ```turtle
   fhkb:isNephewOf a owl:ObjectProperty ;
       rdfs:domain fhkb:Man ;
       rdfs:range fhkb:Person ;
       owl:propertyChainAxiom ( fhkb:isSonOf fhkb:isSiblingOf ) .
   ```

   > 说明（示例代码）：`isSonOf`、`isSiblingOf` 都已在 onto.ttl 中定义（见 [L122](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/2-Symbolic/data/onto.ttl#L122)、[L139-L143](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/2-Symbolic/data/onto.ttl#L139-L143)），所以这条链能被推理引擎展开。

3. **重新载入并做闭包**：确保你的载入 cell 读的是**加了新规则的**那份 `onto.ttl`，然后重新执行 `DeductiveClosure(OWLRL_Extension).expand(g)`。注意：Notebook 第 12 个 cell 会**追加**事实到 `onto.ttl`，所以若你直接在拷贝里加规则，要保证规则在追加事实**之前**就写好（或单独维护一个含规则的完整文件再载入）。

4. **新增一个 SPARQL 查询 cell**，仿照 [FamilyOntology.ipynb:L515-L527](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/2-Symbolic/FamilyOntology.ipynb#L515-L527)，把 `isUncleOf` 换成 `isNephewOf`：

   ```python
   # 示例代码
   qres = g.query(
       """SELECT DISTINCT ?aname ?bname
          WHERE {
             ?a fhkb:isNephewOf ?b .
             ?a rdfs:label ?aname .
             ?b rdfs:label ?bname .
          }""")
   for row in qres:
       print("%s is nephew of %s" % row)
   ```

5. **清理**：Notebook 最后一个 cell 会 `!rm onto.ttl`，确保实验不留垃圾。

**需要观察的现象与预期结果**：

- **闭包后**三元组数应比原来的 4246 **更多**（因为多了一族 `isNephewOf` 三元组被推导出来）。
- **查询**应打印出若干「X is nephew of Y」的行——这些是原本 `isUncleOf` 查询里**完全不会出现**的「从侄子视角看」的关系，正是「原本查不到的亲属关系」。
- 如果打印为空，最可能的原因是 `isSonOf`/`isSiblingOf` 在事实层没实例（注意 Notebook 第 12 个 cell 主要写的是 `isMotherOf/isFatherOf/isBrotherOf/isSisterOf`，`isSonOf/isSiblingOf` 是否被推理出来依赖本体里 `Man/Person` 等类的定义是否触发）。遇到这种情况，可改用更稳的链 `( hasSon isSiblingOf )` 或 `( isSonOf isBrotherOf )` 等价形式再试，并标注「待本地验证」。

**进阶（可选）**：再新增对称的 `isNieceOf`（侄女/外甥女，链 `( isDaughterOf isSiblingOf )`，`domain = Woman`），并对比男女两版的查询结果数量。

> 提醒：本任务只改你工作目录里的副本和 Notebook 新增 cell，**不要修改仓库源码** `data/onto.ttl` 或现有 cell。

## 6. 本讲小结

- 符号 AI 围绕两件事展开：**知识表示**（把知识写成机器可用的数据）与**推理**（让机器自动得出结论）；其中要区分数据/信息/知识/智慧（DIKW），「数据 ≠ 知识」。
- 知识表示是一条谱：左端算法刻板、右端自然语言不可推理，中间的折中包括语义网络、OAV 三元组、框架、产生式规则、描述逻辑；**本体**是对一个领域显式、形式化的规约，语义网用 RDF/OWL 三元组 + URI 把它铺到全网。
- **专家系统**三件套是工作记忆、知识库、推理引擎，核心是「知识与推理分离」；推理分 **前向**（事实驱动，如 `Animals.ipynb` 的 Experta）和 **反向**（目标驱动，如自研的 `KnowledgeBase.get/eval`），区别在于提问顺序与效率。
- `FamilyOntology.ipynb` 演示了完整链条：GEDCOM 家谱 → 三元组 → 与本体合并 → `DeductiveClosure` 闭包扩展（三元组 669 → 4246）→ SPARQL 查询，其中 `isUncleOf` 等关系是**纯靠属性链规则推导**出来的。
- **概念图**有狭义（Microsoft Concept Graph，从文本挖掘、带概率的 `is-a` 图）与广义（语义网络）两层含义；前者是「挖」的，后者是「写」的。
- 符号 AI 的独特价值是**可解释**——每条结论都能追溯是哪条规则推出的；这也是它在需要解释、可控修改的真实项目里仍有一席之地的原因。

## 7. 下一步学习建议

- **衔接下一讲 u2-l3（感知机）**：本讲是符号主义的终点，下一讲正式进入连接主义。建议对比体会：专家系统的「权重」是人类写的 0/1 规则，而感知机的权重是**从数据里学出来的实数**——这正是两种范式的分水岭。
- **继续阅读的源码**：
  - 想深挖语义网，可读 [data/onto.ttl](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/2-Symbolic/data/onto.ttl) 全文，把每条 `propertyChainAxiom` 都翻译成中文，并尝试用 [Protégé](https://protege.stanford.edu/) 可视化打开。
  - 想体验从文本挖概念图，跑一下本目录的 [MSConceptGraph.ipynb](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/2-Symbolic/MSConceptGraph.ipynb)（给新闻分类）。
  - 想做写作型作业，完成本课的 [assignment.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/2-Symbolic/assignment.md)——用 Protégé 自建一个小型本体。
- **拓展阅读**：README 末尾的 Review 建议了解 Bloom 教学分类、林奈生物分类法、门捷列夫元素周期表——它们都是人类「给世界建立本体」的经典尝试，能帮你在更大尺度上理解知识表示的意义。
