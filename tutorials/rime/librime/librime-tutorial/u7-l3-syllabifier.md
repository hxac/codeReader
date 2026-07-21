# Syllabifier 与音节图

## 1. 本讲目标

本讲承接 u7-l2（Calculus 与拼写运算）。上一篇我们知道了「拼写代数」如何在**构建期**把原始拼写变换成一组派生拼写（模糊音、缩写、纠错等）并写进 Prism。本讲要回答的是**运行期**的核心问题：

> 用户在输入框里敲下一串字母（比如 `xian`），librime 如何把它切成「所有可能的音节组合」，并把结果交给词典去查候选？

学完本讲，你应当能够：

1. 说清楚 `SyllableGraph` 这张图里 `vertices`、`edges`、`indices` 三张表各自记录了什么。
2. 手动推演 `BuildSyllableGraph` 对一段输入的 BFS（广度优先）搜索过程，包括它是如何借助 Prism 的 `CommonPrefixSearch` / `ExpandSearch` 来「认识」音节的。
3. 解释三个开关——`delimiters`（分隔符）、`enable_completion`（补全）、`strict_spelling`（严格拼写）——分别改变什么行为。
4. 理解纠错 `Corrector` 是如何在搜索阶段「容错」的，以及 `CheckOverlappedSpellings` 如何标记「歧义切分点」（例如 `xian` 既可以整体读 `xian`，也可以拆成 `xi/an`）。

本讲只讲「把输入串变成音节图」这一步；图建好之后如何沿图查词典、生成候选，属于 u8 词典系统的内容。

---

## 2. 前置知识

在进入源码前，先用通俗语言建立几个直觉。

### 2.1 为什么要「切音节」

拼音输入里，用户敲的 `nihao` 其实是两个音节 `ni` 和 `hao` 拼在一起。引擎不能假设用户会用空格把音节分开——绝大多数人就是一气呵成地敲 `nihao`。所以引擎必须自己判断：这串字母到底由哪些音节组成？

这件事的难点在于**歧义**。以 `xian` 为例，至少有两种合理读法：

| 读法 | 音节序列 | 含义 |
|------|----------|------|
| 整体 | `xian`（仙/先/…） | 一个音节 |
| 拆分 | `xi` + `an`（西安） | 两个音节 |

两种读法都可能对应到用户想打的字，所以引擎不能二选一，而是要**把所有合理的切法都保留下来**，让词典去查每种切法，最后由候选排序来决定哪个更可能。`Syllabifier`（音节切分器）就是负责「枚举所有合理切法」的组件。

### 2.2 用「图」来同时表达多种切法

表达「多种切法」最自然的数据结构是**有向无环图（DAG）**：

- 把输入串的每个**字符位置**当作一个顶点（vertex）。位置 0 是起点，位置 N（输入长度）是终点。
- 每个音节是从某个位置 `start` 到另一个位置 `end` 的一段，画成一条**边**（edge）。
- 从起点到终点的**任意一条路径**，就对应一种合法的音节切法。

以 `xian`（4 个字符）为例，顶点是位置 0、1、2、3、4：

```
0 --xi--> 2 --an--> 4        （读法：xi + an = 西安）
0 --xian-> 4                 （读法：xian = 仙）
```

这样一张图同时容纳了两种切法。本讲的主角 `SyllableGraph` 就是这种图的具体内存表示。

### 2.3 Prism：音节的「字典」

切分器本身并不知道哪些字母组合是合法音节——这件事交给 `Prism`（棱镜）。Prism 是一个基于双数组 trie（Darts 库）的索引：构建时把所有合法音节的拼写（含拼写代数派生出的变体）插进去，运行时提供两个关键查询（详见 u8-l2）：

- `CommonPrefixSearch(input)`：从 `input` 的开头，找出所有「是某个音节前缀」的串及其长度。
- `ExpandSearch(input, limit)`：以 `input` 为前缀，向后扩展，找出更长的匹配（用于「补全」）。

切分器每次站在某个位置上，就用 `CommonPrefixSearch` 问 Prism「从这儿往后，能凑出哪些音节？」，得到的每个音节就变成图上的一条边。

### 2.4 可信度（credibility）回顾

上一篇 u7-l1 讲过，每条拼写都带一个 `credibility`（对数可信度，0 最佳，越小越差）。不同来源的拼写可信度不同：完整拼写 0，模糊/缩写约 `log(0.5)`，补全 `log(0.05)`，纠错 `log(0.01)`。本讲会看到这些数值如何被注入到图的边上。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/rime/algo/syllabifier.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/syllabifier.h) | 声明 `SyllableGraph` 数据结构与 `Syllabifier` 类接口 |
| [src/rime/algo/syllabifier.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/syllabifier.cc) | `BuildSyllableGraph` 的全部实现，含 BFS、补全、纠错、歧义检测、转置 |
| [src/rime/algo/spelling.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/spelling.h) | `SpellingType` 枚举与 `SpellingProperties`（边的属性直接复用它） |
| [src/rime/dict/prism.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/prism.h) | `Prism` 索引与 `SpellingAccessor` 访问器（切分器调用的查询接口） |
| [src/rime/dict/corrector.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/corrector.h) | `Corrector` 纠错抽象接口 |
| [test/syllabifier_test.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/test/syllabifier_test.cc) | 配套单元测试，含 `tuan`/`changan` 等歧义切分样例 |

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：①`SyllableGraph` 数据结构；②`BuildSyllableGraph` 的 BFS 主流程；③分隔符/补全/严格拼写三个开关；④纠错与歧义检测。

### 4.1 SyllableGraph：用图描述「一段输入的所有切法」

#### 4.1.1 概念说明

`SyllableGraph` 是 `BuildSyllableGraph` 的**输出**，也是下游词典查询的**输入**。它把「一段输入串的所有合理音节切法」压缩进三张表：

- **顶点表 `vertices`**：记录「哪些字符位置被访问过」，以及每个位置上「到达它的最佳拼写类型」。
- **边表 `edges`**：核心。记录从位置 `start` 出发、能一步跨到哪些位置 `end`，跨过去对应哪些音节（`SyllableId`）和什么属性。
- **索引表 `indices`**：`edges` 的「转置」视图，方便下游按「起点 + 音节」快速定位。

#### 4.1.2 核心流程

三张表的关系可以用下面这张图理解（以 `xian` 为例）：

```
输入位置:  0   1   2   3   4
           x   i   a   n

edges（邻接表）:
  edges[0] = { 2: {xi->props},  4: {xian->props} }
  edges[2] = { 4: {an->props} }

vertices: { 0: kNormalSpelling, 2: kAmbiguousSpelling, 4: kNormalSpelling }

indices（转置后，按「起点+音节」查）:
  indices[0][id(xi)]   = [ &edge(0->2) ]
  indices[0][id(xian)] = [ &edge(0->4) ]
  indices[2][id(an)]   = [ &edge(2->4) ]
```

注意：`edges` 的键是 `(start, end, syllable_id)`，而 `indices` 的键是 `(start, syllable_id)`、值是该音节在不同 `end` 处的所有属性指针列表。转置后，下游只要问「在位置 `start`、音节 `id` 处，有哪些可能的结束位置？」就能一次拿到全部答案。

#### 4.1.3 源码精读

先看 `SyllableId` 的定义和边的属性类型 [src/rime/algo/syllabifier.h:L21-L33](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/syllabifier.h#L21-L33)：音节在 librime 内部统一用一个 32 位整数 `SyllableId` 表示（它在 Prism/Table 里就是音节表的行号）；边上的属性 `EdgeProperties` 直接继承 `SpellingProperties`，并多加一个字段 `ambiguous_source_positions`（后面讲歧义检测会用）。

四组类型别名层层嵌套，是读懂边表的关键 [src/rime/algo/syllabifier.h:L30-L37](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/syllabifier.h#L30-L37)：

- `SpellingMap = map<SyllableId, EdgeProperties>`——从「位置 `end`」出发，每个音节对应的属性。
- `EndVertexMap = map<size_t, SpellingMap>`——键是 `end` 位置，值是上面的 `SpellingMap`。
- `EdgeMap = map<size_t, EndVertexMap>`——键是 `start` 位置，值是上面的 `EndVertexMap`。

于是 `edges[start][end][syllable_id]` 就拿到一条边的属性，正好对应「从 `start` 到 `end`、拼成音节 `syllable_id`」。

最后是结构体本身 [src/rime/algo/syllabifier.h:L39-L45](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/syllabifier.h#L39-L45)：`input_length` 是原始输入长度，`interpreted_length` 是「图能解释到的最远位置」——如果它小于输入长度，说明尾部有字符没被任何音节吃掉（输入不完整）。

`Syllabifier` 类本身的接口很简洁 [src/rime/algo/syllabifier.h:L47-L70](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/syllabifier.h#L47-L70)：构造时传入三个配置（分隔符串、是否补全、是否严格拼写），核心入口就一个 `BuildSyllableGraph(input, prism, graph)`，外加一个 `EnableCorrection` 用来挂载纠错器。

#### 4.1.4 代码实践

**实践目标**：用真实测试确认 `edges` 的「`(start, end, syllable_id)` 三级嵌套」结构。

**操作步骤**：

1. 打开 [test/syllabifier_test.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/test/syllabifier_test.cc)，定位 `CaseChangan` 测试（第 81 行起）。该测试用一组写死的音节（`a`、`an`、`cha`、`chan`、`chang`、`gan`、…）构建 Prism，再切分输入 `changan`。
2. 阅读第 96–111 行的断言，注意它如何逐层下钻：
   - `g.edges[0]` 拿到从位置 0 出发的所有边（`EndVertexMap`）；
   - `e0[4]` 拿到「0→4」这条边上的 `SpellingMap`；
   - `e0[4].find(syllable_id_["chan"])` 确认音节 `chan` 在其中。

**预期现象**：对 `changan`，测试断言 `edges[0]` 同时有到位置 4（`chan`）和到位置 5（`chang`）的边，`edges[4]` 有到 7（`gan`）的边，`edges[5]` 有到 7（`an`）的边——也就是说图同时保留了 `chan+gan` 和 `chang+an` 两种切法。

3. 把这四种访问层级 `edges[0] → e0[4] → e0[4][id]` 抄成一句中文，描述「位置 0 到位置 4、拼成 chan」这条边是如何被定位的。

#### 4.1.5 小练习与答案

**练习 1**：`interpreted_length` 一定等于 `input_length` 吗？举例说明何时不等。

> **答案**：不一定。当输入尾部有字符凑不成任何音节时（例如 `ang` 里 `g` 凑不成音节，只有 `an` 被认出），`interpreted_length` 会小于 `input_length`。`CaseFailure` 测试就断言 `input.length() - 1 == g.interpreted_length`。

**练习 2**：为什么 `edges` 用三层 `std::map` 嵌套，而不是一个 `struct { start, end, syllable_id, props }` 的扁平数组？

> **答案**：因为切分过程是「从某个 `start` 位置往前探索」，需要按 `start` 快速取到「能去哪些 `end`」；而下游查图又需要按 `(start, syllable_id)` 取属性。`map` 嵌套天然提供这两条按键查找的路径，且键自动有序，便于在「转置」和「清理废边」时按位置遍历。

---

### 4.2 BuildSyllableGraph：BFS 搜索 + Prism 前缀查询

#### 4.2.1 概念说明

`BuildSyllableGraph` 是本讲的「大脑」。它的策略是：以位置 0 为起点，不断问 Prism「从当前位置往后能凑出哪些音节？」，每凑出一个音节，就在图上画一条到 `end` 的边，并把 `end` 作为新的探索点继续往下走——这正是**广度优先 / 最短路径风格**的搜索。由于「更优的拼写类型」应当优先被访问（这样较差的类型可以被丢弃），它用了一个**按拼写类型排序的优先队列**。

#### 4.2.2 核心流程

主循环的伪代码（省略分隔符与纠错细节）：

```
把 (位置=0, 类型=kNormalSpelling) 压入优先队列 queue
farthest = 0
while queue 非空:
    (current_pos, vertex_type) = queue 中类型最优的顶点
    若 current_pos 已被访问过: continue   # 丢弃较差的重复到达
    记录 vertices[current_pos] = vertex_type（首次到达即最优类型）
    farthest = max(farthest, current_pos)

    跳过 current_pos 之后的分隔符，得到真正开始匹配的 begin_pos
    matches = prism.CommonPrefixSearch( input[begin_pos:] )   # 所有前缀音节
    对每个 match（长度 m）:
        end_pos = begin_pos + m + 尾部可能再跳过的分隔符
        对该音节在 Prism 中的每一个 spelling（经 QuerySpelling）:
            把 (syllable_id, props) 写入 edges[current_pos][end_pos]
        把 (end_pos, 该边上最优类型) 压入 queue
```

优先队列里比较的是 `(位置, 拼写类型)` 这个 `pair`，`std::greater` 使其成为**小根堆**：先比位置（小者优先，符合「先处理靠前位置」），位置相同时比类型（`kNormalSpelling=0` 最优，先处理）。因此一个位置第一次被弹出时，携带的就是「到达它的最优拼写类型」，之后再到达的较差类型会被第 46–51 行的 `continue` 丢弃。

#### 4.2.3 源码精读

优先队列与顶点的定义 [src/rime/algo/syllabifier.cc:L19-L21](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/syllabifier.cc#L19-L21)：`Vertex = pair<size_t, SpellingType>`，`VertexQueue` 是基于它的 `priority_queue<..., std::greater<Vertex>>`。

两个对数空间的常量会在补全和纠错时注入 [src/rime/algo/syllabifier.cc:L23-L28](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/syllabifier.cc#L23-L28)：补全惩罚 `kCompletionPenalty = log(0.05) ≈ -3.0`，纠错可信度 `kCorrectionCredibility = log(0.01) ≈ -4.6`。注释里给出了「权重阶梯」直觉：全拼最好（0）、简拼（缩写）次之（≈ −2.3）、补全再次（≈ −3.0）。

入口与起点初始化 [src/rime/algo/syllabifier.cc:L30-L38](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/syllabifier.cc#L30-L38)：空输入直接返回 0；否则把起点 `(0, kNormalSpelling)` 压入队列。

主循环顶部：弹出最优顶点、首次访问才记录、丢弃较差重复 [src/rime/algo/syllabifier.cc:L40-L55](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/syllabifier.cc#L40-L55)。注意第 46–51 行的 `continue`——这是「一个顶点只保留最优类型」的关键，注释里被注释掉的那行 `std::min` 说明：曾经考虑过「保留更优类型」的合并，但最终改为「首次即最优、后来者直接丢弃」。

跳过分隔符 + 调用 Prism 做前缀搜索 [src/rime/algo/syllabifier.cc:L57-L69](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/syllabifier.cc#L57-L69)：先吃掉当前位置之后的一串分隔符得到 `begin_pos`（`leading_gap` 记录吃掉了几个），再用 `input.substr(begin_pos)` 去 `CommonPrefixSearch`。

把每个匹配展开成边 [src/rime/algo/syllabifier.cc:L88-L150](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/syllabifier.cc#L88-L150)：对每个 `match`，先算出 `end_pos`（含吃掉尾部分隔符，第 94–98 行），再经 `prism.QuerySpelling(m.value)` 拿到「该拼写对应的所有音节及属性」逐个写入 `spellings`（第 106–135 行）。这里有一个细节：一个拼写可能映射到**多个音节**（因为拼写代数会把模糊音/缩写派生出来），所以要用 `SpellingAccessor` 循环遍历。

「严格拼写」把关在第 110–113 行（详见 4.3 节）。第 122–127 行处理同一 `(end, syllable)` 重复到达时取**更优**的 `type`（`std::min`）。第 144–147 行把 `end_vertex_type` 用「路径上最差类型」钳位后压入队列——这正是第 142–143 行注释里 `shurfa` 例子要说明的：一条路径的「顶点类型」取沿途最差的一档。

搜索结束后，还有三步收尾（后面三节展开）：清理废顶点/废边（4.2 收尾）、补全（4.3）、转置（本节末）。

转置 `Transpose` 在最后统一执行 [src/rime/algo/syllabifier.cc:L252-L254](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/syllabifier.cc#L252-L254)，把 `edges` 转成 `indices`，函数返回 `farthest`（即 `interpreted_length`）。

「清理废顶点/废边」是 BFS 之后的一遍**反向扫描** [src/rime/algo/syllabifier.cc:L154-L202](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/syllabifier.cc#L154-L202)：从最远可达位置 `farthest` 往回扫，用 `good` 集合标记「确实能连到终点」的位置；任何终点不在 `good` 里的边被删除（第 164–169 行）；同时在同一条边上，若存在「更优类型路径」，就把较次类型的拼写删掉（第 172–185 行）；最后连一条边都不剩的顶点也被删（第 194–199 行）。这一步保证了图里每条边都「有用」，没有死胡同。

#### 4.2.4 代码实践

**实践目标**：手动推演 BFS 如何对 `changan` 同时生成 `chan+gan` 与 `chang+an` 两条路径。

**操作步骤**：

1. 假设音节表为 `CaseChangan` 里 SetUp 注入的那一组（`a, an, cha, chan, chang, gan, han, hang, na, tu, tuan`，经排序后各获一个 `SyllableId`）。
2. 模拟主循环：
   - 起点 0：`CommonPrefixSearch("changan")` 命中 `cha`(3)、`chan`(4)、`chang`(5)。于是产生边 `0→3`、`0→4`、`0→5`，把 3、4、5 压入队列。
   - 处理 3（`cha`）：从 `"ngan"` 继续，凑不出音节，没有出边——`3` 在清理阶段会被删（注释「not c'han'gan」正是此意）。
   - 处理 4（`chan`）：`CommonPrefixSearch("gan")` 命中 `gan`(3)，产生边 `4→7`。
   - 处理 5（`chang`）：`CommonPrefixSearch("an")` 命中 `an`(2)，产生边 `5→7`。
   - 处理 7：到末尾，结束。
3. 对照 [test/syllabifier_test.cc:L96-L111](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/test/syllabifier_test.cc#L96-L111) 的断言核对你的推演：`edges[0]` 应到 4 和 5（注意没有到 3，因为 3 是死路被清掉了）、`edges[4]` 到 7（`gan`）、`edges[5]` 到 7（`an`）。

**预期结果**：`vertices.size() == 4`（位置 0、4、5、7），`interpreted_length == 7 == input.length()`。两种切法 `chan+gan`、`chang+an` 都在图里。

#### 4.2.5 小练习与答案

**练习 1**：为什么主循环用「优先队列 + 首次到达即记录」而不是普通 FIFO 队列？

> **答案**：因为同一个位置可以被多条不同质量的路径到达（例如既可由 `kNormalSpelling` 到达、也可由 `kAbbreviation` 到达）。优先队列保证「最优类型先被处理并记录」，之后较差类型在第 51 行被 `continue` 丢弃，从而每个顶点只保留最优类型，避免劣质切法污染图。

**练习 2**：第 144–146 行有一句 `if (end_vertex_type < vertex.second) end_vertex_type = vertex.second;`，它在做什么？

> **答案**：把「这条边到达端点的类型」用「当前顶点自身已经拥有的（更差的）类型」向下取齐。含义是：一条路径的端点类型不能优于它起点的类型——如果到达 `current_pos` 时已经是较次的拼写类型，那么从它延伸出去的边端类型也应至少这么次。这保证了沿路径的「瓶颈类型」被正确传播。

---

### 4.3 分隔符、补全与严格拼写

#### 4.3.1 概念说明

`Syllabifier` 构造时的三个参数控制切分的边界行为：

- **`delimiters`**（分隔符串，如 `" '"`）：用户（或上游 segmentor）可能在音节之间插入空格、撇号等。切分器要把这些字符**透明地跳过**，不把它们算进音节。注意：分隔符不是「强制切分点」，只是「可忽略字符」。
- **`enable_completion`**（补全）：当用户还没敲完一个音节时（例如只敲了 `ni` 就想看候选），允许切分器用 `ExpandSearch` 把 `ni` 当成 `ni+...` 的前缀，补全成完整音节 `ni`(你/尼…)。补全出的拼写会被打上 `kCompletion` 类型、扣 `log(0.05)` 惩罚。
- **`strict_spelling`**（严格拼写）：当输入**恰好整体**等于一个音节时，禁止把它解释为模糊音或缩写——避免「用户明明打全了一个字，却被当成简拼」。

#### 4.3.2 核心流程

三个开关在主流程中的位置：

```
分隔符:  在每个位置「吃掉」前后分隔符（leading_gap / 尾部 while）
补全:    BFS 主体结束后，若 farthest < input.length()，
         对 input[farthest:] 做 ExpandSearch，把候选音节补成一条到 input.length() 的边
严格拼写: 在写入边属性前判断，
         若 (current_pos==0 && end_pos==input.length()) 且属性非 kNormalSpelling，则跳过该属性
```

补全的惩罚公式（对数空间）：

\[
\text{credibility}_{\text{completion}} = \text{credibility}_{\text{orig}} + \log(0.05)
\]

#### 4.3.3 源码精读

**分隔符**：在主循环里，从 `current_pos` 向后吃掉分隔符得到 `begin_pos` [src/rime/algo/syllabifier.cc:L57-L63](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/syllabifier.cc#L57-L63)，`leading_gap` 记录吃掉的个数用于修正 `end_pos`（第 88、94 行）；匹配出音节后，再从 `end_pos` 向后吃掉尾部分隔符 [src/rime/algo/syllabifier.cc:L95-L98](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/syllabifier.cc#L95-L98)。这样 `xi an`（中间有空格）和 `xian` 在图上的音节边是一致的，区别只是边的跨度把空格也含进去了。

**严格拼写**：判断条件 `matches_input` 表示「这条边正好覆盖整段输入」 [src/rime/algo/syllabifier.cc:L100-L113](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/syllabifier.cc#L100-L113)。当 `strict_spelling_ && matches_input` 且该属性不是 `kNormalSpelling` 时，进入空的 `else` 分支——即**不写入**这条属性。注释解释：禁止把整段输入解释成「模糊拼写或缩写」作为一个单词。

**补全**：BFS 主体（含清理废边）之后，若开启了补全且 `farthest` 还没到输入末尾 [src/rime/algo/syllabifier.cc:L204-L245](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/syllabifier.cc#L204-L245)：对剩余串 `input.substr(farthest)` 调用 `ExpandSearch`（上限 512，第 206–208 行），对每个扩展出的拼写，经 `QuerySpelling` 遍历音节，把类型强改为 `kCompletion`、可信度加 `kCompletionPenalty`，并写入一条从 `farthest` 直接到 `input.length()` 的边（第 215–235 行）。注意它只接受原本类型优于 `kAbbreviation` 的拼写（第 225 行），避免把缩写再当成补全叠加。

#### 4.3.4 代码实践

**实践目标**：直观感受分隔符被「吃掉」的效果。

**操作步骤**：

1. 打开 [test/syllabifier_test.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/test/syllabifier_test.cc) 的 `TrimLeadingDelimiters` / `TrimTrailingDelimiters` / `TrimBothLeadingAndTrailingDelimiters`（第 164 行起），这三个测试构造 `Syllabifier s(" '")`（即把空格和单引号都当分隔符），输入分别是 `''a`、`a''`、`''a''`。
2. 阅读断言：三种情况下都只剩两个顶点（0 和末尾位置 3/3/5），且 `edges[0][末尾]` 里只有音节 `a`。也就是说前导/尾随的分隔符都被跳过，`a` 这条边从位置 0 直接跨到输入末尾。

**预期结果**：分隔符不影响「能切出哪些音节」，只影响「边的跨度位置」。`interpreted_length` 始终等于完整 `input.length()`（分隔符也被算进解释长度）。

**补全的「源码阅读型」实践**：在 `BuildSyllableGraph` 第 204 行的条件 `enable_completion_ && farthest < input.length()` 处设想：若用户在拼写方案里关掉了补全（`speller` 配置不设或设 `enable_completion: false`），这段代码不会执行，于是输入 `ni`（如果 `ni` 恰好是合法音节则没问题；若不是）将得不到任何候选——这解释了为什么补全开关对「边打边出候选」体验至关重要。（运行行为待本地验证，取决于具体方案的音节表与 `speller` 配置。）

#### 4.3.5 小练习与答案

**练习 1**：分隔符被「吃掉」后，`edges[0][3]` 里的 `3` 是音节长度还是输入位置？

> **答案**：是**输入位置**（`end_pos`）。例如 `''a`（长度 3）里 `a` 这条边的 `end_pos = 3`，是它在原输入串里的绝对字节位置，而不是音节 `a` 的长度 1。`leading_gap` 和尾部 while 循环正是用来把分隔符也算进这个位置。

**练习 2**：补全产生的拼写类型为什么必须是 `kCompletion` 而不能保留 `kNormalSpelling`？

> **答案**：因为补全意味着「用户还没敲完、算法在猜」，其可信度天然低于用户完整输入的拼写。统一打 `kCompletion`（并扣 `log(0.05)` 惩罚）后，下游候选排序会让「完整匹配」优先于「补全匹配」，避免把猜测结果排到真实输入前面。

---

### 4.4 纠错（Corrector）与歧义检测（CheckOverlappedSpellings）

#### 4.4.1 概念说明

剩下两件事都让图「更丰富」也更「智能」：

- **纠错 `Corrector`**：用户敲错了某个字母（例如把 `ni` 敲成 `mi`）。如果只靠精确前缀搜索，`mi` 查不到 `ni` 就出不来候选。纠错器在搜索阶段做**容错查询**（基于编辑距离等），把「相近」的拼写也当成命中，但给这些边打上 `is_correction` 标记、可信度压到 `log(0.01)`，让它们排在真实匹配之后。
- **歧义检测 `CheckOverlappedSpellings`**：处理像 `xian`（= `xi`+`an`，也 = `xian`）这种「一个长音节恰好等于两个短音节拼接」的情况。算法在图中找到这种「重叠」后，把中间那个顶点标记为 `kAmbiguousSpelling`，并在受影响的边上记录 `ambiguous_source_positions`，供下游（词典/翻译器）在排序时**惩罚歧义切分**。

#### 4.4.2 核心流程

**纠错**在主循环的「前缀搜索」之后追加一步：

```
matches = prism.CommonPrefixSearch(...)        # 精确前缀
if corrector_ 已启用:
    记下精确命中的音节集合 exact_match_syllables
    corrections = corrector_->ToleranceSearch(prism, input, tolerance=5)  # 容错命中
    对每个容错命中、且其本身有 kNormalSpelling 拼写的音节:
        把它当作一条额外的 match 加入 matches
# 在写入边属性时:
    若该音节不在 exact_match_syllables 中: props.is_correction = true; credibility = log(0.01)
```

**歧义检测**在「清理废边」阶段被调用：对每条「类型优于 `kAbbreviation`」的边 `(start, end)`，检查是否存在 `Y`、`X` 使得 `start --Y--> joint --X--> end` 且 `start --(Y+X)--> end`（即长音节 `Z=Y+X` 恰好跨越了 `joint`）。若存在，就把 `joint` 标记为 `kAmbiguousSpelling`，并在 `X` 边的属性里记下 `ambiguous_source_positions.insert(start)`。

#### 4.4.3 源码精读

**纠错命中注入**：在 `CommonPrefixSearch` 之后 [src/rime/algo/syllabifier.cc:L70-L86](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/syllabifier.cc#L70-L86)：先用 `exact_match_syllables` 记下精确命中，再调 `corrector_->ToleranceSearch(prism, current_input, &corrections, 5)`（容错上限 5）。对每个容错结果，用 `prism.QuerySpelling` 找到它的一个「真实的 `kNormalSpelling` 且非纠错」的拼写，作为一个额外 match 加入（第 76–85 行）。注意 `Corrector::ToleranceSearch` 是抽象接口 [src/rime/dict/corrector.h:L59-L62](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/corrector.h#L59-L62)，具体实现（如 `EditDistanceCorrector` / `NearSearchCorrector`）决定容错策略。

**纠错属性标记**：在写入边属性时 [src/rime/algo/syllabifier.cc:L117-L121](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/syllabifier.cc#L117-L121)：若该音节不在 `exact_match_syllables` 里，就把 `is_correction=true`、`credibility=kCorrectionCredibility`。这样下游一眼就能挑出「这是纠错来的」边。注意第 130 行计算 `end_vertex_type` 时排除了纠错边（`!props.is_correction`），避免纠错边影响顶点的「正常」类型判定。

**歧义检测入口**：在清理废边的循环里，对每条「类型优于缩写」的存活边调用一次 [src/rime/algo/syllabifier.cc:L189-L190](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/syllabifier.cc#L189-L190)。

**歧义检测实现** [src/rime/algo/syllabifier.cc:L257-L289](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/syllabifier.cc#L257-L289)：函数开头的注释把语义说得很清楚——「if "Z" = "YX", mark the vertex between Y and X an ambiguous syllable joint」。代码枚举从 `start` 出发的每个 `Y`（终点 `joint`），再看从 `joint` 出发是否有 `X` 恰好到达 `end`；若有，则把 `X` 边上每个拼写的 `ambiguous_source_positions` 插入 `start`（第 279–282 行），并把 `vertices[joint]` 置为 `kAmbiguousSpelling`（第 283 行）。注释举的反例是 `niju'ede` 这类容易切错的拼音。

**纠错启用**：外部通过 `EnableCorrection(corrector)` 注入纠错器 [src/rime/algo/syllabifier.cc:L303-L305](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/syllabifier.cc#L303-L305)，仅赋值一个指针，主循环据此判断是否走容错分支。

#### 4.4.4 代码实践

**实践目标**：用 `CaseTuan` 验证歧义检测，并迁移到 `xian`。

**操作步骤**：

1. 阅读 [test/syllabifier_test.cc:L114-L138](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/test/syllabifier_test.cc#L114-L138) 的 `CaseTuan`：输入 `tuan`，音节表含 `tu`、`an`、`tuan`。断言：
   - `vertices[2] == kAmbiguousSpelling`（位置 2 是 `tu` 和 `an` 的接缝，因为 `tuan = tu+an`）；
   - `vertices[4] == kNormalSpelling`；
   - `edges[0]` 同时到 2（`tu`）和 4（`tuan`）；`edges[2]` 到 4（`an`）。
2. **迁移到 `xian`**（本讲综合实践的核心，先在这里预演）：假设把音节表换成含 `xi`、`an`、`xian`、`xia` 的一组，输入 `xian`。按 `CheckOverlappedSpellings` 的逻辑，`start=0`、`Y=xi`(到 joint=2)、`X=an`(从 2 到 end=4)，且存在长音节 `xian` 从 0 直接到 4（即 `Z=Y+X`）。因此：
   - `vertices[2]` 应被标为 `kAmbiguousSpelling`；
   - `edges[0]` 含 `0→2`(xi)、`0→4`(xian)（可能还有 `0→3`(xia)，但 `xia` 之后凑不出音节，会在清理阶段被删）；
   - `edges[2]` 含 `2→4`(an)，其 `an` 拼写的 `ambiguous_source_positions` 含 `{0}`。
3. （可选，本地实验）仿照 `CaseTuan` 写一个 `TEST_F`，但注意 SetUp 里的音节表是写死的，要换 `xian` 需要另起一个独立的测试夹具或修改本地副本——**不要提交这个改动**，仅用于观察。

**预期结果**：歧义点 `2` 被标 `kAmbiguousSpelling`，图同时保留 `xian`（整体）与 `xi+an`（拆分）两种路径。这正是切分器「不二选一、全部保留」的设计意图。

#### 4.4.5 小练习与答案

**练习 1**：纠错边（`is_correction=true`）在「清理废边」阶段受到什么特殊对待？

> **答案**：在第 174–176 行，凡是 `is_correction` 的拼写一律 `continue` 跳过、不参与「类型比较淘汰」。也就是说纠错边不会被「存在更优类型路径」这一规则删掉，从而保留了纠错候选；但它们也不计入 `edge_type`（第 178 行条件），不影响正常边的存废判定。

**练习 2**：`CheckOverlappedSpellings` 为什么只在 `edge_type < kAbbreviation`（即类型足够好）时才调用？

> **答案**：歧义检测针对的是「正常拼写」级别的切分歧义（如 `tuan`/`tu+an`）。如果一条边本身只是缩写或更次的类型，其切分本就不可靠，再讨论「重叠歧义」意义不大，反而可能误标。故只在较优类型上做检测，保证标记的歧义点是有意义的。

---

## 5. 综合实践

**任务**：完整推演并验证 `Syllabifier` 对输入 `xian` 的切分，把本讲四个模块串起来。

**背景设定**：假设有一份拼音方案的 Prism，其音节表至少包含 `xi`、`xia`、`xian`、`an`（每个音节都有一个 `SyllableId`），且未启用拼写代数派生（即每个拼写只映射到自身音节，`type=kNormalSpelling`、`credibility=0`）。分隔符取默认空串、不开补全、不开严格拼写、不开纠错。

**步骤**：

1. **画出 BFS 推进过程**（对应 4.2）：
   - 起点 0：`CommonPrefixSearch("xian")` 命中 `xi`(2)、`xia`(3)、`xian`(4) → 产生边 `0→2`、`0→3`、`0→4`，压入 2、3、4。
   - 处理 2：`CommonPrefixSearch("an")` 命中 `an`(2) → 边 `2→4`。
   - 处理 3：`CommonPrefixSearch("n")` 无命中 → 无出边（顶点 3 稍后会被清理）。
   - 处理 4：到末尾。
2. **预测清理后的图**（对应 4.2 收尾 + 4.4 歧义检测）：
   - `vertices`：`{0: kNormal, 2: ?, 4: kNormal}`；顶点 3 因无出边被删。
   - 歧义检测在边 `(0,4)`（`xian`）上触发：枚举 `Y`，发现 `Y=xi`(joint=2)、`X=an`(2→4) 满足 `Z=Y+X`，故 `vertices[2] = kAmbiguousSpelling`，`edges[2][4][id(an)].ambiguous_source_positions = {0}`。
3. **写下转置后的 `indices`**（对应 4.1）：
   - `indices[0][id(xi)] = [&edge(0→2)]`
   - `indices[0][id(xian)] = [&edge(0→4)]`
   - `indices[2][id(an)] = [&edge(2→4)]`
4. **用测试印证**（对应 4.4 实践）：对照 `CaseTuan` 的断言结构（[test/syllabifier_test.cc:L114-L138](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/test/syllabifier_test.cc#L114-L138)），写出 `xian` 版本应有的等价断言（`vertices.size()==3`、`vertices[2]==kAmbiguousSpelling`、`vertices[4]==kNormalSpelling`、`edges[0]` 同时含 2 与 4、`edges[2][4]` 含 `an`）。
5. **反思**：解释为什么 `xian` 的歧义标记对输入法体验有意义——它让「西安」(`xi+an`) 和「仙」(`xian`) 的候选都能被词典查到，再由词频和歧义惩罚决定先后。

**交付物**：一张 `xian` 的音节图手绘图 + 三条 `indices` 条目 + 一段说明「歧义点为何被标记」。图的下游消费（沿 `indices` 查 Table）留到 u8-l5 再展开。

---

## 6. 本讲小结

- `SyllableGraph` 用三张表描述一段输入的所有合理切法：`vertices`（位置→最优类型）、`edges`（`start→end→syllable→props` 三级邻接表）、`indices`（`edges` 的转置，按 `start+syllable` 查）。
- `BuildSyllableGraph` 是一次「优先队列驱动的 BFS」：每个位置用 Prism 的 `CommonPrefixSearch` 找出所有前缀音节画成边，靠「首次到达即最优、后来者丢弃」让每个顶点只保留最优拼写类型。
- BFS 之后还有三步收尾：反向扫描清理死路与次优边、（可选）补全、最后 `Transpose` 生成 `indices`。
- 三个开关——`delimiters`（吃掉可忽略字符）、`enable_completion`（用 `ExpandSearch` 补全、打 `kCompletion` 类型扣 `log(0.05)`）、`strict_spelling`（禁止把整段输入解释为模糊/缩写）——分别控制边界、未完成输入与整体匹配的行为。
- 纠错 `Corrector` 在前缀搜索后追加 `ToleranceSearch` 容错命中，给这些边打 `is_correction` 并压可信度到 `log(0.01)`，且在清理阶段豁免淘汰。
- `CheckOverlappedSpellings` 检测「长音节 = 两短音节之和」的切分歧义（如 `xian`= `xi`+`an`），把接缝顶点标为 `kAmbiguousSpelling` 并在边上记录歧义来源，供下游排序时惩罚。
- 所有可信度都工作在对数空间：完整 0、缩写/模糊 `log(0.5)`、补全 `log(0.05)`、纠错 `log(0.01)`，使「概率相乘」退化为「可信度相加」。

---

## 7. 下一步学习建议

本讲产出的 `SyllableGraph` 是「输入串」与「词典查询」之间的桥梁，但它自己不查任何字。接下来建议：

1. **先读 u8-l1（词典系统总览）**：了解 `Prism`/`Table`/`ReverseDb` 三种产物是怎么从 `.dict.yaml` 编出来的，理解 `SyllableId` 的真正出处。
2. **再读 u8-l2（Prism）**：精读 `CommonPrefixSearch` / `ExpandSearch` / `QuerySpelling` 的双数组 trie 实现，把本讲当成黑盒调用的那几个方法打开。
3. **然后读 u8-l5（Dictionary 查询主链路）**：看 `Dictionary::Lookup(SyllableGraph)` 如何沿本讲生成的 `indices` 把音节序列翻译成 `DictEntryCollector`，完成「按键→候选」的最后一步。
4. 若对纠错感兴趣，可顺带阅读 `src/rime/dict/corrector.cc` 中 `EditDistanceCorrector::ToleranceSearch` 与 `NearSearchCorrector` 的两种容错策略实现。
