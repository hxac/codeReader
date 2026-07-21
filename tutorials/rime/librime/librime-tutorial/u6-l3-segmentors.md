# Segmentor 组件族：输入串如何被切成带标签的片段

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚 `Segmentor` 这一组件基类的契约（`Proceed(Segmentation*)` 返回 bool 的含义）。
- 解释引擎在 `CalculateSegmentation` 中如何用「轮次 + `Forward()`」驱动一串 segmentor 协作切分。
- 逐个理解 `abc_segmentor` / `ascii_segmentor` / `matcher` / `affix_segmentor` / `fallback_segmentor` 的切分逻辑，以及它们各自写入的 tag。
- 看懂「tag → translator」的绑定关系：被切出来的片段靠 tag 决定由哪个翻译器去查候选。
- 用一段具体输入（如 `P:nihao`）手工推演整条切分链路，并解释 `recognizer` 的正则模式如何作为「前缀识别」的闸门。

## 2. 前置知识

本讲是 `u6-l1 引擎流水线总览` 的直接续篇，前置认知请直接承接：

- **Composition / Segmentation / Segment**：`Context::composition_` 内部是一串首尾相接的 `Segment`（`[start, end)` 半开区间），无遗漏、无重叠地覆盖整段输入。详见 `u3-l2 Segmentation 与 Segment`。
- **Segment 的四态状态机**：`kVoid / kGuess / kSelected / kConfirmed`；`tags` 是一个 `set<string>`，可叠加多个值。详见 `u3-l2`。
- **流水线两阶段**：处理器（Processor）只改 `Context`（通常是把按键追加进 `input`）；输入变化经 `update_notifier_` 触发 `Compose`，`Compose` 内部分 `CalculateSegmentation`（本讲主题）与 `TranslateSegments`（下一讲 `u6-l4`）。详见 `u6-l1`。
- **组件注册与 Ticket**：方案 YAML 的 `engine/segmentors` 列表里写的是「处方串」，引擎用 `Ticket` 拆解（`klass@name_space`）后 `Require`+`Create` 实例化。详见 `u5-l1`~`u5-l4`。

一个一句话直觉：**Processor 负责「把字符塞进输入框」，Segmentor 负责「把输入框里的字符串切成若干段、并给每段贴上 tag 标签」**，tag 再决定后续哪个 Translator 去这段里查候选词。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/rime/segmentor.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/segmentor.h) | `Segmentor` 抽象基类，定义 `Proceed` 纯虚函数与统一构造契约。 |
| [src/rime/segmentation.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/segmentation.h) / [src/rime/segmentation.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/segmentation.cc) | `Segmentation` 容器与 `Segment` 结构；`AddSegment` 三规则、`Forward`、各类 `GetCurrent*Position`。 |
| [src/rime/engine.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc) | `ConcreteEngine::CalculateSegmentation` —— 驱动 segmentor 链的主循环。 |
| [src/rime/gear/abc_segmentor.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/abc_segmentor.cc) | 按字母表切分拼音/形码串，打 `abc` 标签。 |
| [src/rime/gear/ascii_segmentor.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/ascii_segmentor.cc) | ASCII 模式下把整段输入直通为 `raw` 段。 |
| [src/rime/gear/matcher.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/matcher.cc) / [src/rime/gear/recognizer.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/recognizer.cc) | `matcher` 是个 segmentor，复用 recognizer 的正则模式给片段打 tag（如 `pinyin`/`url`/`email`）。 |
| [src/rime/gear/affix_segmentor.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/affix_segmentor.cc) | 把带前缀/后缀的输入（如 `P:...;`）剥成 `xxx_prefix` / `xxx` / `xxx_suffix` 多段。 |
| [src/rime/gear/fallback_segmentor.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/fallback_segmentor.cc) | 兜底：谁都没认领的字符变成单字符 `raw` 段。 |
| [src/rime/gear/gears_module.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/gears_module.cc) | 注册了上述所有 segmentor 的名字。 |
| [data/minimal/luna_pinyin.schema.yaml](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.schema.yaml) | `engine/segmentors` 清单与 `recognizer/patterns`，是本讲实践的对照样本。 |

## 4. 核心概念与源码讲解

### 4.1 Segmentor 的契约与协作机制

#### 4.1.1 概念说明

`Segmentor` 是引擎流水线四类组件之一（另三类是 Processor / Translator / Filter）。它的工作面非常窄：**拿到一个 `Segmentation*`，往里面塞 `Segment`，并返回一个 bool**。它不直接产出候选词（那是 Translator 的事），只负责「切分 + 贴 tag」。

和四类组件基类完全对称的设计是：都继承 `Class<T, const Ticket&>`、都以 `Ticket` 构造、都持有 `engine_` 和 `name_space_`，唯一差异是核心纯虚函数。对 Segmentor 而言，这个函数就是 `Proceed`。

#### 4.1.2 核心流程

切分不是「一个 segmentor 一次性把整段输入切完」，而是**多轮、多 segmentor 协作**：

1. 引擎 `CalculateSegmentation` 在「当前起点位置」开启一轮。
2. 按 `engine/segmentors` 清单的顺序，**依次**调用每个 segmentor 的 `Proceed`。
3. 每个 `Proceed` 返回 bool：
   - 返回 `true` → 「本轮我还可以继续，请把机会给下一位 segmentor」。
   - 返回 `false` → 「本轮到此为止，立刻跳出 segmentor 循环」。
4. 一轮结束后，若起点位置推进了，就 `Forward()` 开启下一轮，直到覆盖到输入末尾（`HasFinishedSegmentation`）。
5. 关键防死循环保护：若一整轮下来起点位置没推进（没有任何 segmentor 认领），立刻 `break`。

`AddSegment` 用三条规则把各 segmentor 在**同一起点**的表态归并：

- **规则一（左对齐）**：只接受与当前起点左对齐的段；起点不符直接拒绝。
- **规则二（贪心最长）**：保留 `end` 更大的段，覆盖较短的。
- **规则三（等长合并 tag）**：长度相同时，把两段的 `tags` 求并集。

这就让多个 segmentor（如 `abc_segmentor` 与 `matcher`）能在同一段上叠加各自的 tag，而不是互相覆盖。

#### 4.1.3 源码精读

**基类契约**——[src/rime/segmentor.h:18-31](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/segmentor.h#L18-L31)：`Segmentor` 继承 `Class<Segmentor, const Ticket&>`，构造时从 ticket 摘出 `engine_` 与 `name_space_`，核心是一个纯虚 `Proceed(Segmentation*)`。

```cpp
class Segmentor : public Class<Segmentor, const Ticket&> {
 public:
  explicit Segmentor(const Ticket& ticket)
      : engine_(ticket.engine), name_space_(ticket.name_space) {}
  virtual bool Proceed(Segmentation* segmentation) = 0;
  ...
};
```

**驱动主循环**——[src/rime/engine.cc:171-201](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L171-L201)：外层 `while (!HasFinishedSegmentation())`，内层 `for (auto& segmentor : segmentors_)` 依次 `Proceed`，谁返回 `false` 就 `break`；轮末若 `start_pos == GetCurrentEndPosition()`（没人推进）也 `break`，避免死循环。

```cpp
for (auto& segmentor : segmentors_) {
  if (!segmentor->Proceed(segments))
    break;
}
// no advancement
if (start_pos == segments->GetCurrentEndPosition())
  break;
...
if (!segments->HasFinishedSegmentation())
  segments->Forward();   // 开下一轮
```

**AddSegment 三规则**——[src/rime/segmentation.cc:84-111](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/segmentation.cc#L84-L111)：分别对应「起点不符拒绝」「保留更长段」「等长合并 tag」。等长合并正是多 tag 叠加的来源：

```cpp
} else {  // rule three: with segments equal in length, merge their tags
  set<string> result;
  set_union(last.tags.begin(), last.tags.end(),
            segment.tags.begin(), segment.tags.end(), ...);
  last.tags.swap(result);
}
```

**Forward 的语义**——[src/rime/segmentation.cc:114-120](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/segmentation.cc#L114-L120)：在当前段末尾追加一个**空的占位段** `[end, end)`，作为下一轮的起点。注意 `Forward` 只在「当前段非空」时才推进。

#### 4.1.4 代码实践

**实践目标**：看清「轮次」与 `Proceed` 返回值的协作。

**操作步骤**：
1. 打开 [src/rime/engine.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc) 的 `CalculateSegmentation`。
2. 回答：`Proceed` 返回 `false` 时，是跳出 `for` 还是跳出 `while`？（答案：只跳出内层 `for`，外层 `while` 仍可能继续，前提是位置已推进。）
3. 在 `affix_segmentor.cc` 与 `fallback_segmentor.cc` 里搜索 `return false` 与 `return true`，体会「独占式切分」与「协作式切分」的区别。

**预期结果**：你会看到 `affix_segmentor` 成功剥离前缀后 `return false`（独占，不让后续 segmentor 碰这段），而 `abc_segmentor` 总是 `return true`（只切自己负责的部分，把机会让给别人）。

#### 4.1.5 小练习与答案

**练习 1**：如果某个方案的 `segmentors` 列表里没有任何 segmentor 能认领当前字符，会发生什么？
**答案**：一轮下来 `start_pos == GetCurrentEndPosition()`，触发 `break` 跳出 `while`，切分提前结束，未覆盖的尾部字符不会被翻译（这正是为何 `fallback_segmentor` 几乎总排在最后兜底）。

**练习 2**：`Forward()` 为什么只在「当前段非空」时才追加新段？
**答案**：见 [segmentation.cc:115](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/segmentation.cc#L115) `if (empty() || back().start == back().end) return false;`——若当前还是空占位段就 `Forward`，会塞进一串无意义的空段，且无法推进覆盖输入。

---

### 4.2 abc_segmentor：按字母表切分，打 abc 标签

#### 4.2.1 概念说明

`abc_segmentor` 是拼音/形码方案里**最主力**的切分器：它按方案的「字母表」（`speller/alphabet`）把连续的合法拼写字符归成一段，并打上 `abc` 标签。`abc` 是个约定俗成的「默认拼写段」tag——`script_translator` / `table_translator` 默认就消费 `abc` 段。

它还理解「声母/韵母期待」和「分隔符」：拼音里 `'`（撇号）和空格是音节分隔符，不算拼写字符但允许出现在段内。

#### 4.2.2 核心流程

`abc_segmentor::Proceed` 从当前起点 `j` 开始向后扫：

1. 逐字符判断：是否为字母表内字符（`is_letter`）？是否为分隔符（`is_delimiter`，且非首字符）？
2. 既不是字母也不是分隔符 → 立即停止扫描。
3. 维护一个 `expecting_an_initial`（是否期待下一个是声母）：若期待声母但当前字符既非声母也非分隔符 → 停止（不是一个合法拼写开头）。
4. 扫描结束得到区间 `[j, k)`，若 `j < k` 就新建 `Segment(j, k)`，插入 `abc` 标签（以及配置里的 `extra_tags`），`AddSegment`。
5. 总是 `return true`（让出机会）。

#### 4.2.3 源码精读

**字母表默认值**——[src/rime/gear/abc_segmentor.cc:13](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/abc_segmentor.cc#L13)：默认字母表是**小写字母倒序**串 `"zyxwvutsrqponmlkjihgfedcba"`。注意它是倒序写的（不是为了排序，而是作者随手列出），实际匹配只看「字符是否在串里」，与顺序无关。

**从方案读配置**——[src/rime/gear/abc_segmentor.cc:17-37](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/abc_segmentor.cc#L17-L37)：从 `speller/alphabet`、`speller/delimiter`、`speller/initials`、`speller/finals` 读取，并支持 `abc_segmentor/extra_tags` 追加额外标签。若 `initials` 为空则默认等于整个字母表（即任意字母都能做声母）。

**扫描主循环**——[src/rime/gear/abc_segmentor.cc:39-69](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/abc_segmentor.cc#L39-L69)：

```cpp
for (; k < input.length(); ++k) {
  bool is_letter = alphabet_.find(input[k]) != string::npos;
  bool is_delimiter = (k != 0) && (delimiter_.find(input[k]) != string::npos);
  if (!is_letter && !is_delimiter) break;          // 遇到非法字符停止
  ...
  if (expecting_an_initial && !is_initial && !is_delimiter) break;  // 不合法开头
  expecting_an_initial = is_final || is_delimiter;  // 韵母/分隔符之后才可接新声母
}
if (j < k) {
  Segment segment(j, k);
  segment.tags.insert("abc");
  for (const string& tag : extra_tags_) segment.tags.insert(tag);
  segmentation->AddSegment(segment);
}
return true;  // 让出机会
```

> 注意 `is_final`（韵母判断）依赖 `speller/finals`；luna_pinyin 方案未配置 `finals`，所以 `expecting_an_initial` 始终为 `false`，等于「任意位置都可接字母」，对纯字母串表现为「一路吞到非字母为止」。

#### 4.2.4 代码实践

**实践目标**：手工推演 `nihao` 的切分。

**操作步骤**：
1. 确认 [luna_pinyin.schema.yaml:71](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.schema.yaml#L71) 的 `alphabet: zyxwvutsrqponmlkjihgfedcba`、`delimiter: " '"`。
2. 假设输入是 `nihao`（5 个小写字母）。模拟 `abc_segmentor::Proceed`：从 `j=0` 扫，每个字符都在字母表内、无分隔符、`expecting_an_initial` 恒为 false，一直扫到 `k=5`。
3. 得到 `Segment(0, 5)`，插入 `abc`。

**预期结果**：切分结果为一段 `[{0,5} {abc}]`。这个 `abc` 段随后会交给默认的 `script_translator`（其默认 tag 就是 `abc`）去查词典。

#### 4.2.5 小练习与答案

**练习 1**：输入 `ni'hao`（带撇号）会被切成几段？
**答案**：仍然是一段 `[{0,6} {abc}]`。因为 `'` 在 `delimiter` 里，`is_delimiter` 为真，不会中断扫描；撇号被视为段内的音节分隔符留在同一段中。

**练习 2**：为什么大写字母 `P` 不会被 `abc_segmentor` 切入？
**答案**：默认字母表只含小写字母，`P` 不在其中，`is_letter` 与 `is_delimiter` 都为假，循环立即 `break`，`j==k` 故不产生段——这正是大写开头输入要靠 `matcher`（见 4.3）或 `fallback`（见 4.5）处理的原因。

---

### 4.3 ascii_segmentor 与 matcher：模式识别与 ASCII 直通

#### 4.3.1 概念说明

不是所有输入都该走「按字母表拼成词」这条路。两类 segmentor 负责把输入「按规则整段认领」：

- **`ascii_segmentor`**：当 `ascii_mode` 选项打开时（用户切到西文模式），把**整段剩余输入**直接当作一个 `raw` 段——意思是「原样上屏，不要查词典」。
- **`matcher`**：一个用正则表达式识别特殊输入的 segmentor，复用了 `recognizer`（它本身也是个 Processor）的模式表。它能把 URL、邮箱、大写单词，或带前缀的指令（如 `P:...`）整段识别出来，并打上对应 tag（如 `url`/`email`/`uppercase`/`pinyin`）。

#### 4.3.2 核心流程

**ascii_segmentor** 极简：

1. 若 `ascii_mode` 未开 → 直接 `return true`（不做事，让出机会）。
2. 若开了 → 把 `[当前起点, 输入末尾]` 整段建成一个 `Segment`，打 `raw` 标签，`AddSegment`。
3. `return false`（独占：ASCII 模式下后续 segmentor 无需再切）。

**matcher** 的流程：

1. 构造时从 `recognizer/patterns` 加载「tag → 正则」映射（若 `name_space_` 是默认的 `segmentor`，会改读到 `recognizer`）。
2. `Proceed` 时，对当前活动输入跑一遍所有正则。
3. 关键闸门：只有当某个正则匹配的区间**恰好延伸到输入末尾**（`end == input.length()`）时，才认领它，并打上该正则对应的 tag。
4. 命中则 `AddSegment`，但 `return true`（仍让出机会——它打的 tag 会被 `affix_segmentor` 利用，见 4.4）。

#### 4.3.3 源码精读

**ascii_segmentor 整段直通**——[src/rime/gear/ascii_segmentor.cc:19-30](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/ascii_segmentor.cc#L19-L30)：

```cpp
if (!engine_->context()->get_option("ascii_mode"))
  return true;                       // 非西文模式：让出机会
...
Segment segment(j, input.length());  // 从起点一直包到末尾
segment.tags.insert("raw");
segmentation->AddSegment(segment);
return false;                        // 西文模式：独占，结束本轮
```

**matcher 加载 recognizer 模式**——[src/rime/gear/matcher.cc:15-24](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/matcher.cc#L15-L24)：构造时把 `name_space_` 从默认的 `segmentor` 改写成 `recognizer`，再调 `patterns_.LoadConfig`。

**matcher 的认领逻辑**——[src/rime/gear/matcher.cc:26-42](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/matcher.cc#L26-L42)：调用 `patterns_.GetMatch`，命中则建段并插入 `match.tag`。

**正则闸门在 recognizer 里**——[src/rime/gear/recognizer.cc:39-70](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/recognizer.cc#L39-L70)：`GetMatch` 遍历每条 `tag → 正则`，用 `boost::regex_search` 在「活动输入」上查找；最关键的一行是：

```cpp
if (end != input.length())   // 只认领「顶到输入末尾」的匹配
  continue;
```

这意味着 recognizer 模式本质是「**后缀锚定**」的：模式通常写成 `...$`，且只有匹配尾部时才生效。比如 [default.yaml:131-135](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/default.yaml#L131-L135) 的 `uppercase: "[A-Z][-_+.'0-9A-Za-z]*$"`，会把大写开头的整串识别为 `uppercase` 段。

> 顺带一提：`recognizer` 同时是个 **Processor**（[recognizer.cc:84-102](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/recognizer.cc#L84-L102)），它在按键阶段采用「先试后写」——把候选字符拼到当前 `input` 后面试匹配，命中才 `PushInput`。这与本讲的 segmentor 视角互补：Processor 决定「这个字符要不要进输入框」，matcher segmentor 决定「进了输入框后整串属于哪个 tag」。

#### 4.3.4 代码实践

**实践目标**：理解「正则必须顶到末尾才会认领」这一闸门。

**操作步骤**：
1. 打开 [luna_pinyin.schema.yaml:153-159](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.schema.yaml#L153-L159)，找到 `pinyin: "P:[a-z']*;?$"`。
2. 思考输入 `P:ni`：正则 `P:[a-z']*;?$` 在 `P:ni` 上匹配，`end=3==length`，命中 → 打 `pinyin` tag。
3. 再思考输入 `P:ni hao`（含空格）：`[a-z']*` 无法吃掉空格，匹配只到 `P:ni`，`end=3 != length(8)` → 不命中。这一差异是下一节 `affix_segmentor` 能否生效的关键。

**预期结果**：你会直观看到「空格不在 `[a-z']*` 字符类里」导致 `P:ni hao` 整串不被识别为 `pinyin` 前缀输入，从而走完全不同的切分路径。

#### 4.3.5 小练习与答案

**练习 1**：`ascii_segmentor` 与 `matcher` 都可能给整段输入打 tag，它们的「触发条件」有何根本区别？
**答案**：`ascii_segmentor` 只看一个布尔 option（`ascii_mode`），是用户手动切换的全局模式；`matcher` 看输入串本身的正则特征，是内容驱动的自动识别。

**练习 2**：为什么 `matcher` 命中后仍 `return true` 而不是 `false`？
**答案**：因为它打的 tag（如 `pinyin`）需要被排在它后面的 `affix_segmentor@pinyin` 进一步加工（剥离前缀），故必须把机会让出去；它只是「贴标签」，不是「独占切分」。

---

### 4.4 affix_segmentor：前缀/后缀剥离与多段切分

#### 4.4.1 概念说明

`affix_segmentor` 解决「带前缀的指令式输入」。典型场景是反查与多方案混用：用户输入 `P:nihao;` 表示「用拼音反查」，`C:abcd;` 表示「临时用仓颉」。这类输入有一个**前缀**（如 `P:`）、一段**正文编码**、一个可选**后缀**（如 `;`）。

`affix_segmentor` 通过 `Ticket` 的 `name_space`（即处方串里 `@` 后面的别名）读取各自的配置，因此同一个 `affix_segmentor` 类可以用 `affix_segmentor@pinyin`、`affix_segmentor@cangjie` 实例化多次，各管各的前缀。

它的工作方式很特别：**它不自己发现前缀，而是依赖 `matcher` 先把整段打上 tag**。只有当当前段已有它的 `tag_`（如 `pinyin`）时，它才动手把前缀/后缀剥成独立小段。

#### 4.4.2 核心流程

以 `affix_segmentor@pinyin`（前缀 `P:`、后缀 `;`、tag `pinyin`）为例，假设 `matcher` 已把 `P:nihao` 整段打上 `pinyin` tag：

1. 检查当前段是否有 `pinyin` tag；没有 → 直接 `return true`（不归我管）。
2. 取活动输入 `active_input`，确认它以 `prefix_`（`P:`）开头；否则 `return true`。
3. **仅前缀**（`active_input` 就是 `P:`）：原地修改该段——抹掉 `pinyin` tag、加上 `pinyin_prefix` tag、设置提示文本，`return true`。
4. **前缀 + 正文**：
   - 把原段 `pop_back`，新建一个 `[j, j+前缀长)` 的小段，状态 `kGuess`、tag `pinyin_prefix` + `phony`（`phony` 表示「不要原样上屏」），设提示文本。
   - 正文部分建成 `[j+前缀长, k)` 段，tag `pinyin`（+ `extra_tags`）。
5. **正文 + 后缀**：若正文以 `suffix_`（`;`）结尾，再把后缀剥成一个 `[k, k+后缀长)` 的 `kGuess` 段，tag `pinyin_suffix` + `phony`。
6. `return false`（独占：剥完就结束本轮）。

结果：`P:nihao` 被切成 `[{0,2} pinyin_prefix,phony] + [{2,7} pinyin]`；若带 `;` 则再加一个后缀段。

#### 4.4.3 源码精读

**配置读取**——[src/rime/gear/affix_segmentor.cc:15-33](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/affix_segmentor.cc#L15-L33)：从 `name_space_/tag`、`/prefix`、`/suffix`、`/tips`、`/closing_tips` 读取。`name_space_` 由 `Ticket` 的别名决定（如 `pinyin`），对应方案里 [luna_pinyin.schema.yaml:112-120](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.schema.yaml#L112-L120) 的 `pinyin:` 段。

**门控：必须有我的 tag**——[src/rime/gear/affix_segmentor.cc:36-51](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/affix_segmentor.cc#L36-L51)：

```cpp
if (segmentation->empty()) return true;
if (!segmentation->back().HasTag(tag_)) {
  // 处理 partial 选择遗留的 tag 继承，否则直接让出
  ...
  return true;
}
```

**仅前缀分支**——[src/rime/gear/affix_segmentor.cc:60-69](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/affix_segmentor.cc#L60-L69)：原地改 tag（`pinyin` → `pinyin_prefix`），不拆段。

**前缀+正文拆分**——[src/rime/gear/affix_segmentor.cc:70-87](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/affix_segmentor.cc#L70-L87)：`pop_back` 原段，依次 `Forward`+`AddSegment` 前缀段与正文段。注意前缀段带 `phony`：

```cpp
prefix_segment.tags.insert(tag_ + "_prefix");
prefix_segment.tags.insert("phony");  // do not commit raw input
```

**后缀剥离**——[src/rime/gear/affix_segmentor.cc:89-105](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/affix_segmentor.cc#L89-L105)：若正文以 `suffix_` 结尾，缩短正文段末尾，再追加一个 `xxx_suffix` + `phony` 段。

> `phony`（「冒牌」）tag 的作用：标记这段（前缀/后缀符号）**没有真实候选、不应原样上屏**。这样即使用户最后没选词直接提交，`P:` 和 `;` 这些指令符号也不会被当作正文输出。

#### 4.4.4 代码实践

**实践目标**：看清 `affix_segmentor` 与 `matcher` 的依赖关系。

**操作步骤**：
1. 在 [luna_pinyin.schema.yaml:49-57](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.schema.yaml#L49-L57) 确认 `segmentors` 列表里 `matcher` 排在 `affix_segmentor@pinyin` **之前**。
2. 思考：如果删掉 `recognizer/patterns/pinyin` 这条正则，`affix_segmentor@pinyin` 还能工作吗？
3. 验证你的结论：`affix_segmentor` 的第一步就是 `HasTag(tag_)`，而 `pinyin` 这个 tag 只有 `matcher` 会打。

**预期结果**：删掉 recognizer 的 `pinyin` 正则后，输入 `P:nihao` 不会被任何 segmentor 打上 `pinyin` tag，于是 `affix_segmentor@pinyin` 直接 `return true`，前缀剥离不会发生——`P:` 会落到 `fallback` 成为 `raw`。

#### 4.4.5 小练习与答案

**练习 1**：为什么前缀段和后缀段都标了 `phony`，而正文段没有？
**答案**：前缀 `P:` 和后缀 `;` 是「指令符号」，不是用户想上屏的文字；正文段（如 `nihao`）才是要翻译成汉字的真内容，不能标 `phony`，否则会被视为无候选而不上屏。

**练习 2**：`affix_segmentor@pinyin` 和 `affix_segmentor@cangjie` 是同一个 C++ 类的两个实例，它们如何区分各自的前缀？
**答案**：靠 `Ticket` 的 `name_space_`（处方串 `affix_segmentor@pinyin` 里的 `pinyin`）去读方案里不同的配置节（`pinyin/prefix` vs `cangjie/prefix`），从而各自得到 `P:` 与 `C:`。这是 `u5-l4` 讲过的「同一组件类借 alias 多次实例化」的典型应用。

---

### 4.5 fallback_segmentor：兜底 raw 段

#### 4.5.1 概念说明

`fallback_segmentor` 是切分链的**最后一道防线**。无论什么字符，只要前面的 segmentor 都没认领，它就把它变成一个单字符的 `raw` 段（「原样上屏」）。这保证了「任何输入都能被切分覆盖」，不会出现输入框里有字符却无段可翻译的尴尬。

它还有一个细节：如果上一个段已经是 `raw`，它会把当前字符**追加**到那个 `raw` 段里，而不是新建一段——这样连续的非法字符会合并成一个 `raw` 段。

#### 4.5.2 核心流程

1. 若当前已有非空段（`GetCurrentSegmentLength() > 0`，即别的 segmentor 已认领）→ `return false`（不用我兜底）。
2. 若起点已到输入末尾 → `return false`。
3. 若尾段是空占位段，先 `pop_back`。
4. 若尾段是 `raw` 段 → 把它的 `end` 扩展一位（吞掉当前字符），`Clear()` 后重打 `raw`（标记需重新翻译），`return false`。
5. 否则新建一个单字符 `Segment(k, k+1)`，打 `raw`，`Forward`+`AddSegment`，`return false`。

#### 4.5.3 源码精读

**完整实现**——[src/rime/gear/fallback_segmentor.cc:16-57](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/fallback_segmentor.cc#L16-L57)：

```cpp
int len = segmentation->GetCurrentSegmentLength();
if (len > 0) return false;           // 别人已认领，不掺和
...
if (!segmentation->empty()) {
  Segment& last(segmentation->back());
  if (last.HasTag("raw")) {          // 续接上一个 raw 段
    last.end = k + 1;
    last.Clear();
    last.tags.insert("raw");         // 标记重新翻译
    return false;
  }
}
{                                    // 否则新建单字符 raw 段
  Segment segment(k, k + 1);
  segment.tags.insert("raw");
  segmentation->Forward();
  segmentation->AddSegment(segment);
}
return false;                        // 兜底总是独占并结束本轮
```

#### 4.5.4 代码实践

**实践目标**：观察 `raw` 段的「续接合并」行为。

**操作步骤**：
1. 假设输入是一个数字 `5`（不在字母表、不匹配任何 recognizer 模式、`punct_segmentor` 也无定义）。
2. 推演：`abc_segmentor` 不认（非字母）→ `matcher` 不认（无模式）→ 各 `affix` 不认 → `punct_segmentor` 不认 → `fallback_segmentor` 兜底建 `[{0,1} {raw}]`。
3. 再假设输入 `52`：第一个 `5` 走完上述流程变成 `raw` 段；继续下一轮到 `2`，`fallback` 发现尾段是 `raw`，把 `end` 从 1 扩到 2，合并成 `[{0,2} {raw}]`。

**预期结果**：连续的非拼写字符会被合并进同一个 `raw` 段。`raw` 段通常由 `echo_translator` 之类原样回显，或在前端表现为「直接上屏」。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `fallback_segmentor` 几乎总排在 `segmentors` 列表的最后？
**答案**：因为它「来者不拒」，只要当前无段就建 `raw`。若排在前面，会把本该由 `abc_segmentor` 切的拼音字符也变成 `raw` 单字符段，切分就乱了。它必须是兜底。

**练习 2**：`fallback` 续接 `raw` 段时为什么要 `last.Clear()` 再重打 `raw`？
**答案**：[fallback_segmentor.cc:42](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/fallback_segmentor.cc#L42) 注释写明「mark redo translation (in case it's been previously translated)」——段变长了，之前缓存的候选已失效，需清空 menu 并把状态打回，强制 Translator 重新翻译。

---

### 4.6 tag 如何指导翻译（把切分与翻译接起来）

#### 4.6.1 概念说明

segmentor 产出的是「带 tag 的段」，但 tag 本身不产生候选。tag 的真正作用是**给 Translator 当筛子**：每个 Translator 在构造时会声明「我消费哪些 tag」，查询时若段的 tag 集合与它声明的 tag 没有交集，就直接返回 `nullptr`（不参与）。这就是「切分指导翻译」的接合点。

#### 4.6.2 核心流程

1. `CalculateSegmentation` 切出若干带 tag 的段。
2. `TranslateSegments` 对每一段，遍历所有 `translators_`，调 `translator->Query(input, segment)`。
3. Translator 内部第一行通常是：`if (!segment.HasAnyTagIn(tags_)) return nullptr;` —— tag 不匹配就退出。
4. tag 匹配的 Translator 才真正查词典、产出 `Translation`，挂到该段的 `Menu`。

#### 4.6.3 源码精读

**Translator 的 tag 门控**——[src/rime/gear/script_translator.cc:211-212](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/script_translator.cc#L211-L212)：

```cpp
if (!segment.HasAnyTagIn(tags_))
  return nullptr;
```

**tags 的来源与默认值**——[src/rime/gear/translator_commons.cc:140-153](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/translator_commons.cc#L140-L153)：从 `translator` 命名空间的 `tag` / `tags` 配置读取；若都没配，默认 `tags_ = {"abc"}`（[translator_commons.h:174](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/translator_commons.h#L174)）。这正是默认 `script_translator` 消费 `abc` 段、而 `script_translator@pinyin` 消费 `pinyin` 段的由来。

> 串起来看一条完整链：用户敲 `P:nihao` → `matcher` 打 `pinyin` tag → `affix_segmentor@pinyin` 剥成 `[pinyin_prefix] + [pinyin]` → `TranslateSegments` 里 `script_translator@pinyin`（tag=pinyin）只对 `[2,7)` 那段响应，查拼音词典 → 候选挂到该段。tag 是贯穿切分与翻译的「暗号」。

#### 4.6.4 代码实践

**实践目标**：验证 tag 与 translator 的对应表。

**操作步骤**：
1. 对照 [luna_pinyin.schema.yaml:58-63](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.schema.yaml#L58-L63) 的 `translators` 列表与各 translator 的 `tag`/`tags` 配置。
2. 列出对应关系：默认 `script_translator`（tag `abc`）← 对应 `abc_segmentor`；`script_translator@pinyin`（tag `pinyin`）← 对应 `matcher`+`affix_segmentor@pinyin` 产出的 `pinyin` 段；`table_translator@cangjie`（tag `cangjie`）← 对应 `P:` 的姊妹前缀 `C:`。

**预期结果**：你会看到「每个 translator 的 tag 都能在 segmentor 侧找到产出者」，形成一一（或多一）对应的闭环。

#### 4.6.5 小练习与答案

**练习 1**：如果想让某段输入只被一个特定 translator 处理，该怎么做？
**答案**：给这段输入一个独有的 tag（如通过 recognizer 自定义模式打 `my_tag`），并让目标 translator 配置 `tags: [my_tag]`，其他 translator 不含该 tag 自然不会响应。

**练习 2**：为什么默认 `script_translator` 不需要配置 `tag` 就能工作？
**答案**：因为 `TranslatorOptions` 在未配置时把 `tags_` 默认初始化为 `{"abc"}`，而 `abc_segmentor` 恰好产出 `abc` 段，二者靠这个默认约定天然对接。

## 5. 综合实践：推演 `P:nihao` 与 `P:ni hao` 的切分差异

本任务把本讲所有模块串起来，重点体会 **recognizer 正则这个闸门** 如何决定 `affix_segmentor` 是否生效。

### 实践目标

手工推演两段输入在 luna_pinyin 方案下的完整切分结果，并用 `rime_api_console` 验证。

### 操作步骤

**第一步：确认装配清单。** 打开 [luna_pinyin.schema.yaml:49-57](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.schema.yaml#L49-L57)，segmentor 顺序为：`ascii_segmentor` → `matcher` → `abc_segmentor` → `affix_segmentor@alphabet` → `affix_segmentor@cangjie` → `affix_segmentor@pinyin` → `punct_segmentor` → `fallback_segmentor`。recognizer 模式见 [:153-159](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.schema.yaml#L153-L159)，其中 `pinyin: "P:[a-z']*;?$"`。

**第二步：推演 `P:nihao`（无空格，正文 7 字符）。**
- 第 1 轮（起点 0）：`matcher` 用 `P:[a-z']*;?$` 匹配 `P:nihao`，`end=7==length`，命中，打 `pinyin`，建段 `[0,7)`，`return true`。随后 `abc_segmentor` 因 `P` 非字母不切；`affix_segmentor@pinyin` 见尾段有 `pinyin` tag，剥前缀 `P:`：`pop_back` 原段，建 `[0,2){pinyin_prefix,phony}`（kGuess，提示「〔拼音〕」）与 `[2,7){pinyin}`，`return false` 结束本轮。
- 覆盖到末尾（7），切分结束。
- **预期结果**：`[{0,2} {pinyin_prefix,phony}] + [{2,7} {pinyin}]`。正文段 `[2,7)`（`nihao`）由 `script_translator@pinyin` 翻译。

**第三步：推演 `P:ni hao`（含空格，8 字符）。**
- 第 1 轮（起点 0）：`matcher` 试 `P:[a-z']*;?$`，但 `[a-z']*` 吃不掉空格，匹配只到 `P:ni`，`end=3 != length(8)` → **不命中**（[recognizer.cc:51](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/recognizer.cc#L51) 的闸门）。其他模式（`uppercase` 匹配 `P`，但 `end=1 != 8` 也不命中）。故 `matcher` 不打 `pinyin`。
- 由于尾段无 `pinyin` tag，`affix_segmentor@pinyin` 直接 `return true`，**前缀剥离不会发生**。
- `P` 非字母 → `abc_segmentor` 不切 → `fallback_segmentor` 把 `P` 兜底成 `raw` 单字符段 `[0,1)`。
- 后续 `:` 可能被 `punct_segmentor`（默认有 `:` 定义）切成 `punct` 段，`ni` 被 `abc_segmentor` 切成 `abc` 段，空格与 `hao` 的归属涉及 `punct_segmentor` 的细节与分隔符处理。

**第四步：用 `rime_api_console` 验证。**
1. 按 `u1-l5` 的方法编译运行 `tools/rime_api_console`，部署 luna_pinyin。
2. 分别输入 `P:nihao` 与 `P:ni hao`，观察 preedit 与候选。
3. 若想看到精确的段与 tag，可在调试构建下（`ENABLE_LOGGING=ON`）打开日志，观察 `DLOG(INFO) << "segmentation: " << *segments;` 的输出（该日志在 [engine.cc:184](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L184) 及各 segmentor 内）。

### 需要观察的现象

- `P:nihao`：preedit 应显示「〔拼音〕」提示并给出 `nihao` 的拼音候选，证明前缀被正确剥离、正文走了 `pinyin` 翻译通道。
- `P:ni hao`：行为截然不同——`P:` 没被识别为前缀指令，`P` 与 `:` 大概率被当作 `raw`/`punct` 原样处理，`ni`、`hao` 走正常拼音切分。

### 预期结果与待验证项

- `P:nihao` 的切分结论（`pinyin_prefix + pinyin` 两段）可由源码逻辑严格推出，**预期成立**。
- `P:ni hao` 中 `:` 与空格的精确段归属依赖 `punct_segmentor` 的具体定义与 recognizer Processor 在按键阶段的「先试后写」交互，**精确边界待本地验证**——这正是本题留给你的实验任务：用 console 或日志确认空格究竟有没有进入 `input`、`:` 落在 `punct` 还是 `raw` 段。

> 一个值得思考的延伸：为什么 recognizer 模式 `P:[a-z']*;?$` 不把空格放进 `[a-z']*`？因为空格在拼音里是音节分隔符，若让前缀识别吞掉空格，就无法在反查时输入多音节词了。这是「模式设计」与「切分语义」的刻意权衡。

## 6. 本讲小结

- `Segmentor` 的契约极简：`Proceed(Segmentation*)` 返回 bool，`true` 让出机会、`false` 独占结束本轮；多个 segmentor 靠「轮次 + `Forward()`」协作。
- `AddSegment` 三规则（左对齐、贪心最长、等长合并 tag）让多个 segmentor 能在同一段叠加 tag，是「切分可组合」的关键。
- `abc_segmentor` 是主力切分器，按 `speller/alphabet` 把合法拼写串切成 `abc` 段，并理解分隔符与声母/韵母期待。
- `ascii_segmentor`（ASCII 模式整段 `raw`）与 `matcher`（正则后缀锚定识别 `url`/`email`/`uppercase`/`pinyin` 等）负责「按规则整段认领」。
- `affix_segmentor` 把带前缀/后缀的指令输入剥成 `xxx_prefix`/`xxx`/`xxx_suffix` 多段，但它依赖 `matcher` 先打 tag，前缀/后缀段标 `phony` 防止原样上屏。
- `fallback_segmentor` 是兜底，把谁都没认领的字符变成单字符 `raw` 段，并能把连续 `raw` 续接合并。
- tag 是贯穿切分与翻译的暗号：Translator 用 `HasAnyTagIn(tags_)` 门控，只有 tag 匹配的段才会被翻译。

## 7. 下一步学习建议

- 本讲只到「段被切好、贴上 tag」，**段如何变成候选词**留待 `u6-l4 Translator 组件族`：重点看 `script_translator` 如何消费 `abc`/`pinyin` 段、`table_translator` 如何消费形码段。
- 想深入理解 recognizer 正则的设计与更多模式（如 `reverse_lookup`），可回头看 `u6-l2` 中 `recognizer` 作为 Processor 的「先试后写」机制，与本讲的 segmentor 视角合在一起才是完整图景。
- 对切分底层结构（`Segment` 四态、`partial` tag、`Reopen`/`Close`）还想巩固的，可重温 `u3-l2 Segmentation 与 Segment`。
- 综合实践中的 `P:n hao` 边界问题，鼓励你实际跑一次 `rime_api_console` 并对照 `src/rime/gear/punctuator.cc` 里的 `PunctSegmentor`（[punctuator.cc:245](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/punctuator.cc#L245)）把验证结果补全。
