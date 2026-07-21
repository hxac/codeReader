# Segmentation 与 Segment

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 `Segmentation` 在 librime 中的角色：它是一段输入串的「**有序切分方案**」，用一串首尾相接的 `Segment` 把整段输入覆盖完。
- 掌握 `Segment` 的数据结构：`[start, end)` 半开区间、`length`、`tags`、`menu`、`selected_index`、`prompt` 各字段的含义与读写时机。
- 画出 `Segment::Status` 的四态状态机（`kVoid / kGuess / kSelected / kConfirmed`），并解释状态如何随切分、用户选择、提交而迁移。
- 理解 `tags` 集合的语义——它是「给这段输入涂上颜色」，决定后续哪个 `Translator` 来翻译它，并能说出 `abc` / `raw` / `punct` / `phony` / `partial` 等常见 tag 是由哪个 segmentor 写入的。
- 看懂 `Forward()` / `Trim()` / `Reset()` / `AddSegment()` 如何协同推进切分边界，以及 `AddSegment` 里「取长段、合并等长段 tag」的三条规则。

本讲是单元 u3（输入状态与候选生成）的第二篇，承接 u3-l1 的 `Context`：上讲我们把 `context_->composition_` 当作黑盒，知道 `Engine::Compose` 会调 `CalculateSegmentation(&comp)` 把它填满。本讲就来拆开这个黑盒的第一层——`Segmentation` 与它装着的 `Segment`。候选如何挂载到 `Segment` 上（`menu` / `Translation` / `Candidate`）留待下一讲 u3-l3。

## 2. 前置知识

在进入源码前，先用三个生活化的比喻建立直觉：

1. **「分词」比喻**：想象用户在键盘上连敲 `nihao`。引擎面对的是一整串字符，但翻译器（Translator）喜欢「一小口一小口」地吃——拼音翻译器只懂 `ni`、`hao` 这种音节，标点翻译器只懂 `,` `.` 这种符号。`Segmentation` 的工作就是把 `nihao` 切成 `[ni][hao]` 这样一段一段，给每段贴上「这是拼音」「这是标点」的标签，再交给对应的翻译器。这和中文分词、英文按空格切分是同一类问题。
2. **「贪心最长匹配 + 投票」比喻**：切分不是某一个人决定的，而是多个 segmentor 轮流发言。每个 segmentor 看着**同一段**输入，给出自己认为的「这段能切多长、是什么类型」。`AddSegment` 的规则是：**取最长的那个方案**，如果几个方案一样长就**把它们的标签合并**（投票）。这是一种「让信息多的赢、并列则都采纳」的策略。
3. **「流水线传送带」比喻**：`Segmentation` 是一条传送带，`Segment` 是带上的格子。`Forward()` 是「把传送带往前推一格，腾出一个新空格」的动作；`Trim()` 是「把带尾多余的空格撤掉」。`Engine::CalculateSegmentation` 就是那个反复「请 segmentor 往格子里放东西 → 推一格 → 请下一段放」的调度员。

此外需要 recall 的术语（来自前几讲）：

- `Context` 是输入状态容器，独占持有 `composition_`（u3-l1）；`composition_` 的静态类型是 `Composition`，而 `Composition` 正是**继承自 `Segmentation`** 的（这点 u3-l3 会确认，本讲先把 `Segmentation` 当作「被 `Context` 持有的切分结果」即可）。
- 流水线四类组件之一是 `Segmentor`，它的契约是 `Proceed(Segmentation* segmentation)`（u5-l2 详述），本讲会把它当作「写 Segment 的客户」来引用。
- `an<T>` 是 `common.h` 里的智能指针别名（u1-l3），`Segment::menu` 的类型就是 `an<Menu>`。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [src/rime/segmentation.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/segmentation.h) | `Segment` 结构体 + `Segmentation` 类声明 | `Segment` 全部字段、`Status` 四态枚举、`Segmentation` 的公共接口 |
| [src/rime/segmentation.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/segmentation.cc) | 实现 | `AddSegment` 三规则、`Forward`/`Trim`/`Reset`、`Close`/`Reopen`、`GetConfirmedPosition`、`operator<<` |
| [src/rime/segmentor.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/segmentor.h) | `Segmentor` 基类契约 | `Proceed(Segmentation*)` 签名，理解「谁在写 Segment」 |
| [src/rime/gear/abc_segmentor.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/abc_segmentor.cc) | 按字母表切分 | 写入 `abc` tag 的真实样例 |
| [src/rime/gear/fallback_segmentor.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/fallback_segmentor.cc) | 兜底切分 | 写入 `raw` tag、单字符推进的真实样例 |
| [src/rime/gear/ascii_segmentor.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/ascii_segmentor.cc) | ASCII 模式整段切分 | `ascii_mode` 下整段标 `raw` 并终止切分 |
| [src/rime/gear/affix_segmentor.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/affix_segmentor.cc) | 前缀/后缀切分 | `phony` / `xxx_prefix` / `xxx_suffix` 等 tag |
| [src/rime/engine.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc) | `ConcreteEngine` | `CalculateSegmentation` 如何驱动一串 segmentor 完成 `Segmentation` |

本讲的「主角源码」是 `segmentation.h` / `segmentation.cc` 两个文件；`gear/` 下的几个 segmentor 与 `engine.cc` 只是用来回答「Segment 是被谁、怎么填进去的」这个必然的追问。这些 segmentor 的内部实现细节属于 u6-l3 的范畴，本讲只取其「写 tag」的片段。

---

## 4. 核心概念与源码讲解

### 4.1 Segmentation：Segment 的有序集合

#### 4.1.1 概念说明

`Segmentation` 解决的问题是：**把一整串原始输入串，切成一串首尾相接的片段，并保证无遗漏、无重叠。**

它的设计非常干脆——直接 `public` 继承自 `std::vector<Segment>`：

```cpp
// segmentation.h:L60
class RIME_DLL Segmentation : public vector<Segment> {
```

也就是说，`Segmentation` **就是一个装着若干 `Segment` 的动态数组**，外加三个东西：

1. 一份对输入串的引用 `input_`（切分是针对这串字符做的）；
2. 一组维护方法（`Reset` / `AddSegment` / `Forward` / `Trim` / `HasFinishedSegmentation` / `Get*Position`）；
3. 一个调试用的 `operator<<`，把整段切分打印成 `[input|s,e{tags}|...]` 的形式。

「首尾相接」是 `Segmentation` 的核心不变式（invariant）：第 `i` 个 Segment 的 `end` 必须等于第 `i+1` 个 Segment 的 `start`，且第一个的 `start` 是 0、最后一个的 `end` 不超过 `input_` 的长度。后面会看到，`Forward()` 正是用来维持这条不变式的。

#### 4.1.2 核心流程

一次完整的切分由 `Engine::CalculateSegmentation` 驱动（[engine.cc:L171-L201](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L171-L201)），骨架是「外层循环推进段、内层循环请 segmentor 发言」：

```
输入串 input_ = "nihao"      （由 Context::composition_::Reset(input) 记下）

外层 while (!HasFinishedSegmentation())：       // 还有没切完的输入
    start_pos = GetCurrentStartPosition()        // 当前空格的起点
    内层 for 每个 segmentor：
        若 segmentor->Proceed(this) 返回 false → break（本段提前定案）
    若 end 没推进 → break（切不动了，避免死循环）
    若还没切完 → segments->Forward()             // 腾出下一个空格

收尾：Trim() 撤掉尾部的空段
```

每一轮内层循环里，各 segmentor 都盯着**同一个起点 `start_pos`** 调 `AddSegment(...)` 表态；`AddSegment` 内部按「取最长、并列合并 tag」的规则把这些表态归并成一个最终 Segment。归并完一段，`Forward()` 把起点往后挪到这段的 `end`，进入下一段。

#### 4.1.3 源码精读

先看 `Segmentation` 的整体声明，建立全局印象：

```cpp
// segmentation.h:L60-L80
class RIME_DLL Segmentation : public vector<Segment> {
 public:
  Segmentation();
  virtual ~Segmentation() {}
  void Reset(const string& input);
  void Reset(size_t num_segments);
  bool AddSegment(Segment segment);

  bool Forward();
  bool Trim();
  bool HasFinishedSegmentation() const;
  size_t GetCurrentStartPosition() const;
  size_t GetCurrentEndPosition() const;
  size_t GetCurrentSegmentLength() const;
  size_t GetConfirmedPosition() const;

  const string& input() const { return input_; }

 protected:
  string input_;
};
```

几个要点：

- `Segmentation` 持有的不是输入串的拷贝动作，而是**记录一份 `input_`**，让 segmentor 能通过 `input()` 读到当前要切的整段字符（[segmentation.h:L76](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/segmentation.h#L76)）。
- `GetCurrentStartPosition()` / `GetCurrentEndPosition()` 返回的都是**当前正在填写的那一格（最后一个 Segment）**的起止位置（[segmentation.cc:L135-L141](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/segmentation.cc#L135-L141)），这是 segmentor 决定「从哪切到哪」的依据。
- `HasFinishedSegmentation()` 判断标准很朴素：最后一个 Segment 的 `end` 是否已经到达 `input_` 的末尾（[segmentation.cc:L131-L133](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/segmentation.cc#L131-L133)）。

那「`input_` 是怎么被记下来的」？在 `Reset(const string& new_input)` 里，它会先做一件聪明事——**增量复用**：

```cpp
// segmentation.cc:L57-L76
void Segmentation::Reset(const string& new_input) {
  DLOG(INFO) << "reset to " << size() << " segments.";
  // mark redo segmentation, while keeping user confirmed segments
  size_t diff_pos = 0;
  while (diff_pos < input_.length() && diff_pos < new_input.length() &&
         input_[diff_pos] == new_input[diff_pos])
    ++diff_pos;
  DLOG(INFO) << "diff pos: " << diff_pos;

  // dispose segments that have changed
  int disposed = 0;
  while (!empty() && back().end > diff_pos) {
    pop_back();
    ++disposed;
  }
  if (disposed > 0)
    Forward();

  input_ = new_input;
}
```

这段代码做的事：找新输入与旧输入的**第一个不同位置 `diff_pos`**，然后把所有 `end > diff_pos` 的 Segment（即受影响的部分）`pop_back()` 丢掉，保留前面不受影响的已确认段。用户每敲一个键只会让输入尾部变化，于是绝大多数情况下只需丢弃最后一段重切，而不必每次都从零切起。这正是「**保留用户已确认的切分，只重做变化部分**」的增量策略。

> 说明：`Reset` 是被 `Composition::Reset` 调用的（在 `Engine::Compose` 开头，[engine.cc:L160](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L160)），本讲把它当作「切分开始前的预处理」即可，`Composition` 的细节在 u3-l3。

最后是调试输出，把整段切分画成一行：

```cpp
// segmentation.cc:L156-L175
std::ostream& operator<<(std::ostream& out, const Segmentation& segmentation) {
  out << "[" << segmentation.input();
  for (const Segment& segment : segmentation) {
    out << "|" << segment.start << "," << segment.end;
    if (!segment.tags.empty()) {
      out << "{";
      ...
      for (const string& tag : segment.tags) { ... out << tag; }
      out << "}";
    }
  }
  out << "]";
  return out;
}
```

比如输入 `nihao` 切成两段拼音，打印出来大概是 `[nihao|0,2{abc}|2,5{abc}]`。这个格式在阅读 segmentor 源码的 `DLOG(INFO) << "segmentation: " << *segmentation;` 时会反复出现，是理解切分行为最直观的工具。

#### 4.1.4 代码实践

**目标**：用 `operator<<` 的输出格式，手工模拟一次 `Segmentation` 的生长过程，建立「`Forward` 腾格子、`AddSegment` 填格子」的肌肉记忆。

**步骤**：

1. 假设输入串 `input_ = "ni,"`（拼音 `ni` + 一个逗号），segmentor 顺序为 `abc_segmentor, punct_segmentor, fallback_segmentor`。
2. 起点：`Segmentation` 初始为空。
3. 第一轮（起点 0）：`abc_segmentor` 看出 `ni` 都是字母，表态 `Segment(0,2)` 带 tag `abc`；`punct_segmentor` 认不得字母，不表态；`fallback` 看到已有非空段也不表态。`AddSegment` 后得到 `[ni,|0,2{abc}]`。
4. `Forward()`：在末尾追加一个空段 `Segment(2,2)`，得到 `[ni,|0,2{abc}|2,2]`，起点变成 2。
5. 第二轮（起点 2）：`abc_segmentor` 看到逗号不是字母，跳过；`punct_segmentor` 认出 `,` 是标点，表态 `Segment(2,3)` 带 tag `punct`。得到 `[ni,|0,2{abc}|2,3{punct}]`。
6. `HasFinishedSegmentation()`：最后一个 Segment 的 `end == 3 == input_.length()`，为真，外层循环结束。`Trim()` 发现尾部没有空段，不动作。

**需要观察的现象**：每一步 `*segmentation` 的字符串表示如何变化；特别留意 `Forward()` 之后那个 `start==end` 的空段是「占位用的脚手架」，最终被 `Trim` 清理。

**预期结果**：最终 `operator<<` 输出 `[ni,|0,2{abc}|2,3{punct}]`，两段首尾相接（0→2→3），正好覆盖整段输入。

> 「待本地验证」：以上为依据源码逻辑的手工推演；若想看真实输出，可在 `segmentation.cc` 的各 `DLOG` 处开启 `ENABLE_LOGGING`（见 u1-l2）后用 `rime_api_console` 输入 `ni,` 观察 `DLOG(INFO) << "segmentation: ..."` 的打印（VLOG/DEBUG 日志属可选项，取决于编译配置）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Segmentation` 要 `public` 继承 `vector<Segment>` 而不是「持有」一个 vector？这样做有什么代价？

**答案**：因为引擎和组件需要频繁地「按索引访问第 i 段」「遍历所有段」「取最后一段」——`Segmentation` 把这些 STL 容器操作直接复用，调用方写 `segments[i]` / `segments.back()` / `for (auto& s : segments)` 即可，无需额外包装。代价是失去了「封装」：任何持有 `Segmentation&` 的代码都能直接 `push_back`/`pop_back` 破坏不变式。代码里用注释和约定（`AddSegment`/`Forward` 是推荐入口）来缓解，属于务实优先的设计。

**练习 2**：`Reset(new_input)` 里为什么要先算 `diff_pos` 再 `pop_back`，而不是无条件清空重切？

**答案**：因为用户连续输入时，新输入通常是旧输入的「前缀 + 末尾追加」，前面已确认/已翻译的段没有变化。保留它们可以避免重复切分与翻译，提升响应速度；更重要的是保留用户已经高亮/选择过的段状态（`kSelected` 等），让编辑体验连贯。只有在真正改变的位置（`diff_pos`）之后才需要重做。

---

### 4.2 Segment 数据结构：区间与字段

#### 4.2.1 概念说明

`Segment` 是 `Segmentation` 里的一个格子，描述**输入串的一个片段**。它需要回答三个问题：

- **这段在输入里的位置**：`start` / `end` / `length`。
- **这段是什么类型**：`tags`（一个字符串集合，是这段的「颜色」）。
- **这段当前的「成熟度」与「产出」**：`status`（四态状态机，见 4.3）、`menu`（候选菜单）、`selected_index`（用户选了第几个）、`prompt`（提示文字）。

一个 Segment 从被创建到被提交，会经历「**空壳 → 有候选但未选 → 用户选了 → 已确认提交**」的成熟过程，对应 `status` 的四态，也对应 `menu` 从无到有。

#### 4.2.2 核心流程

Segment 的字段语义如下表：

| 字段 | 类型 | 含义 | 何时被写 |
| --- | --- | --- | --- |
| `start` / `end` | `size_t` | 输入串里的 `[start, end)` 半开区间 | segmentor 创建 `Segment(j, k)` 时 |
| `length` | `size_t` | `end - start`，冗余但方便 | 构造时算好；`Close` 改 `end` 后需注意不再同步 `length` |
| `status` | `Status` | 四态：`kVoid/kGuess/kSelected/kConfirmed` | segmentor/translator/用户选择分别推进 |
| `tags` | `set<string>` | 这段的「颜色」集合 | segmentor 用 `tags.insert("abc")` 写入；`AddSegment` 合并 |
| `menu` | `an<Menu>` | 这段的候选菜单（智能指针） | Translator 把 `Translation` 包成 `Menu` 挂上 |
| `selected_index` | `size_t` | 用户当前选中的候选下标 | 用户高亮/选择时更新 |
| `prompt` | `string` | 这段的提示文字（如反查模式提示） | `affix_segmentor` 写前缀提示 |

注意「半开区间」这个细节：`Segment(j, k)` 覆盖的是下标 `j, j+1, ..., k-1` 共 `k-j` 个字符，`end` 指向的是「**下一个**字符的位置」。这和 C++ 迭代器区间 `[begin, end)` 完全一致，也是「首尾相接」不变式成立的数学基础：第 i 段的 `end` 就是第 i+1 段的 `start`。

#### 4.2.3 源码精读

`Segment` 是一个普通结构体（不是类，没有访问控制）：

```cpp
// segmentation.h:L18-L32
struct Segment {
  enum Status {
    kVoid,
    kGuess,
    kSelected,
    kConfirmed,
  };
  Status status = kVoid;
  size_t start = 0;
  size_t end = 0;
  size_t length = 0;
  set<string> tags;
  an<Menu> menu;
  size_t selected_index = 0;
  string prompt;
```

所有字段都有默认值，所以默认构造的 `Segment` 是一个「全 0、空 tags、空 menu、`kVoid`」的空壳。两种构造函数：

```cpp
// segmentation.h:L34-L37
Segment() = default;

Segment(int start_pos, int end_pos)
    : start(start_pos), end(end_pos), length(end_pos - start_pos) {}
```

带参构造是 segmentor 最常用的入口：`Segment(j, k)` 直接给定区间，`length` 自动算好（注意 `status` 仍是默认的 `kVoid`，需要时由 segmentor 显式改成 `kGuess`）。

`Clear()` 把一个 Segment 重置回「空壳」，但**保留位置信息**（`start`/`end`/`length` 不动）——因为位置是 segmentor 切好的事实，不应被候选层面的重置抹掉：

```cpp
// segmentation.h:L39-L45
void Clear() {
  status = kVoid;
  tags.clear();
  menu.reset();
  selected_index = 0;
  prompt.clear();
}
```

`fallback_segmentor` 在「把上一段 raw 段延长一个字符」时就调用了 `Clear()`（[fallback_segmentor.cc:L42-L43](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/fallback_segmentor.cc#L42-L43)），目的是清掉旧的候选好让 translator 重算，但位置由随后重新 `tags.insert("raw")` 维护。

最后是两个 tag 查询助手，用于 segmentor/translator 快速判断「这段是不是某种类型」：

```cpp
// segmentation.h:L50-L54
bool HasTag(const string& tag) const { return tags.find(tag) != tags.end(); }
bool HasAnyTagIn(const vector<string>& tags) const {
  return std::any_of(tags.begin(), tags.end(),
                     [this](const string& tag) { return HasTag(tag); });
}
```

例如 `affix_segmentor` 用 `back().HasTag(tag_)` 判断「上一段是不是我要处理的那种」（[affix_segmentor.cc:L38](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/affix_segmentor.cc#L38)）；translator 也靠 `segment.tags` 决定自己要不要为这段产出候选（u3-l3 / u6-l4）。

#### 4.2.4 代码实践

**目标**：理解「半开区间 + 默认 status」的构造语义，并能预测字段初值。

**步骤**：

1. 假设 segmentor 写出 `Segment seg(2, 5); seg.tags.insert("abc");`。
2. 不查源码，先写下你预测的 `seg.start / seg.end / seg.length / seg.status / seg.tags / seg.menu / seg.selected_index` 七个值。
3. 对照 [segmentation.h:L25-L32](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/segmentation.h#L25-L32) 与 [segmentation.h:L36-L37](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/segmentation.h#L36-L37) 核对。
4. 再想：如果把 `seg` 传给 `seg.Clear()`，哪些字段会变？`start`/`end` 会不会变？

**需要观察的现象**：默认构造把 `status` 设成 `kVoid` 而不是 `kGuess`——这意味着「刚切出来、还没翻译」的段是 `kVoid`，要等 translator 填上候选后才升到 `kGuess`（见 `engine.cc::TranslateSegments`）。

**预期结果**：`start=2, end=5, length=3, status=kVoid, tags={"abc"}, menu=nullptr, selected_index=0`。`Clear()` 后 `status=kVoid, tags={}, menu=nullptr, selected_index=0, prompt=""`，但 `start=2, end=5, length=3` 不变。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Segment` 的带参构造只设置 `start/end/length`，却把 `status` 留作默认的 `kVoid`？这反映了什么设计意图？

**答案**：因为「位置」和「成熟度」是两件独立的事。segmentor 只负责切位置（它懂输入串长什么样），不负责产出候选；候选是 translator 的事。把 `status` 默认设为 `kVoid`（「啥都还没有」），让 translator 在填上候选时再把它推进到 `kGuess`，职责清晰。这也解释了为什么 `Clear()` 重置候选但不重置位置——位置事实不该被候选层的操作推翻。

**练习 2**：`Segment::length` 和 `end - start` 是冗余的。指出一处源码里改了 `end` 却没有同步更新 `length` 的地方，说明为什么这样不会出 bug。

**答案**：`Segment::Close()`（[segmentation.cc:L17-L24](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/segmentation.cc#L17-L24)）把 `end = cand->end()` 但没动 `length`；`fallback_segmentor` 延长 raw 段时 `last.end = k + 1` 也不同步 `length`；`Reopen` 里 `end = original_end_pos` 同样如此。之所以不出 bug，是因为切分逻辑主要读 `start`/`end`（`GetCurrentEndPosition`、`HasFinishedSegmentation`、`GetConfirmedPosition` 都用 `end`），`length` 更像构造时的一次性缓存。读者使用时也应优先信赖 `end - start`。

---

### 4.3 Segment 的四态状态机

#### 4.3.1 概念说明

`status` 描述一个 Segment 「**离被提交还有多远**」，是 librime 表达「编辑中间态」的核心。四个状态按「成熟度」递增：

| 状态 | 数值 | 含义 | 典型场景 |
| --- | --- | --- | --- |
| `kVoid` | 0 | 空壳，无候选也没选 | 刚被 segmentor 切出，还没翻译；或候选被 `Clear()` |
| `kGuess` | 1 | 有候选（猜测），用户还没选 | translator 填上 `menu` 后 |
| `kSelected` | 2 | 用户已选中某个候选 | 用户高亮/上屏某个候选，但整段还没提交 |
| `kConfirmed` | 3 | 已确认，等待/已经提交 | 这段的内容已定案，进入提交流程 |

注意 `enum` 的数值是有意递增的，所以源码里常用 `status >= kSelected`（「至少被选中」）、`status < kSelected`（「还没被选中」）这类**区间判断**，而不必逐一列举状态。

#### 4.3.2 核心流程

四态的迁移可以用下面这张图概括（箭头上的标注是触发者/触发动作）：

```
                 segmentor 切出 / Clear()
            ┌─────────────────────────────────────┐
            ▼                                     │
        ┌────────┐  translator 挂上 menu   ┌─────────┐
        │ kVoid  │ ──────────────────────► │ kGuess  │
        └────────┘                         └─────────┘
            ▲                                  │
            │ Reopen(caret_pos) 且             │ 用户选中候选
            │ caret 不在该段末尾                ▼
            │                              ┌──────────┐
            └────────────────────────────  │ kSelected│
                                           └──────────┘
                                                │
                                                │ 提交 / 整段定案
                                                ▼
                                           ┌───────────┐
                                           │ kConfirmed│
                                           └───────────┘

   特殊回边：
   - Reopen(caret_pos) 且 caret == 该段末尾：kSelected/kConfirmed → kGuess（保留候选，继续编辑）
   - Close()：当选中候选只覆盖段的一部分时，把段从选中处「截断」并补 tag "partial"
```

两条「回退」边特别值得注意，它们让用户能**回头修改已经选过的段**：

- `Reopen`：把光标移回到某个已选段时，若光标正好停在段尾，则降回 `kGuess`（候选还在，继续编辑）；若光标停在段中间，则降回 `kVoid`（位置变了，得重切重译）。
- `Close`：当一个段被选中的候选只覆盖了它的一部分时（比如段是 `[0,5)` 但选中的候选只到 `3`），把段截断成「已选部分 + 剩余部分」，剩余部分打上 `partial` tag 留待重切。

#### 4.3.3 源码精读

状态枚举本身就是一张「成熟度阶梯」：

```cpp
// segmentation.h:L19-L24
enum Status {
  kVoid,
  kGuess,
  kSelected,
  kConfirmed,
};
```

「至少被选中」的判断在 `GetConfirmedPosition` 里出现，它扫一遍所有段，找最后一个 `status >= kSelected` 的段的 `end`——即「**用户已经选定的位置**」：

```cpp
// segmentation.cc:L147-L154
size_t Segmentation::GetConfirmedPosition() const {
  size_t k = 0;
  for (const Segment& seg : *this) {
    if (seg.status >= Segment::kSelected)
      k = seg.end;
  }
  return k;
}
```

这个值被 `Engine::Compose` 用来判断「光标是否恰好停在已确认段的边界」（[engine.cc:L161-L165](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L161-L165)），决定要不要把光标后的输入也纳入翻译。

接下来看两条回退边。先看 `Close`——处理「**选中候选只覆盖了段的一部分**」：

```cpp
// segmentation.cc:L15-L24
static const char* kPartialSelectionTag = "partial";

void Segment::Close() {
  auto cand = GetSelectedCandidate();
  if (cand && cand->end() < end) {
    // having selected a partially matched candidate, split it into 2 segments
    end = cand->end();
    tags.insert(kPartialSelectionTag);
  }
}
```

含义：如果用户选中的候选（`GetSelectedCandidate()`）只到 `cand->end()`，而这个位置比段的 `end` 还小，就把段的 `end` 收缩到 `cand->end()`，并打上 `partial` tag。被截掉的那部分输入随后会由 segmentor 重新切分成新的段（`affix_segmentor` 还会继承原 tag，见 [affix_segmentor.cc:L40-L48](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/affix_segmentor.cc#L40-L48)）。这就是「部分匹配候选」的拆段机制。

再看 `Reopen`——用户把光标挪回已选段时的「**复活**」逻辑：

```cpp
// segmentation.cc:L26-L43
bool Segment::Reopen(size_t caret_pos) {
  if (status < kSelected) {
    return false;
  }
  const size_t original_end_pos = start + length;
  if (original_end_pos == caret_pos) {
    // reuse previous candidates and keep selection
    if (end < original_end_pos) {
      // restore partial-selected segment
      end = original_end_pos;
      tags.erase(kPartialSelectionTag);
    }
    status = kGuess;
  } else {
    status = kVoid;
  }
  return true;
}
```

逐行拆解：

- `status < kSelected` 时直接返回 `false`——只有「已被选中」的段才需要重开，没选过的段本来就能随便改。
- `original_end_pos == caret_pos`（光标停在段尾）：降级到 `kGuess`，**保留候选与选择**（注释 `reuse previous candidates and keep selection`）；如果之前是 `partial` 截断的，还把 `end` 恢复成完整长度并擦掉 `partial` tag。
- 否则（光标停在段中间或别处）：降到 `kVoid`，候选作废，等重切重译。

这两个方法生动体现了「状态机」而非「一次性数据」的本质：Segment 是可以被「复活」「截断」「降级」的活对象。

#### 4.3.4 代码实践

**目标**：画出 Segment 的四态迁移图（本讲的主实践之一），并用 `Reopen`/`Close` 的源码为每条边标注触发条件。

**步骤**：

1. 准备一张纸或文本，画出 4 个节点 `kVoid / kGuess / kSelected / kConfirmed`。
2. 画「前进边」：`kVoid → kGuess`（translator 挂 menu）、`kGuess → kSelected`（用户选候选）、`kSelected → kConfirmed`（提交）。标注这些边分别由谁触发（提示：`engine.cc::TranslateSegments` 里 `if (segment.status >= kGuess) continue;` 之后会为 `kVoid` 段填候选并可能设 `kGuess`；用户选择走 `Context::Select`；提交走 `Context::Commit`，u3-l1）。
3. 画「回退边」：基于 [segmentation.cc:L26-L43](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/segmentation.cc#L26-L43) 画 `kSelected/kConfirmed → kGuess`（光标回段尾）和 `→ kVoid`（光标回段中）两条边，写明条件。
4. 画「自修改边」：基于 [segmentation.cc:L17-L24](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/segmentation.cc#L17-L24) 标注 `Close()` 何时收缩 `end` 并加 `partial`。

**需要观察的现象**：状态迁移不是线性的「单向阀门」，而是**带回退的可编辑模型**；这正是输入法「用户随时改主意」体验的底层支撑。

**预期结果**：一张含 4 节点、3 条前进边、2 条回退边、1 条自修改（`Close`）的迁移图，每条边都有触发条件与触发者。

#### 4.3.5 小练习与答案

**练习 1**：源码里为什么大量出现 `status >= kSelected` 而不是 `status == kSelected || status == kConfirmed`？

**答案**：因为 `Status` 枚举的数值是按成熟度递增设计的（`kVoid=0 < kGuess=1 < kSelected=2 < kConfirmed=3`），用 `>=` 一次表达「至少被选中」这种区间语义，比列举更简洁、也更健壮——未来若新增一个介于 `kSelected` 与 `kConfirmed` 之间的状态，`>=` 写法自动兼容，列举写法则会漏掉。这是「把枚举值设计成有序」带来的红利。

**练习 2**：`Reopen` 在 `original_end_pos == caret_pos` 时降到 `kGuess` 而非 `kVoid`，这一选择带来了什么用户体验上的好处？

**答案**：降到 `kGuess` 意味着**保留之前的候选菜单和用户选择**（注释明说 `reuse previous candidates and keep selection`）。好处是：用户把光标移回某段末尾想微调时，不用从头重算候选、不会丢失之前的高亮，编辑体验连贯。只有当光标停在段中间（位置真的变了，候选作废）才降到 `kVoid` 重来。这是「能省则省、按需重算」的增量思想在状态层的体现。

**练习 3**：`Close()` 为什么要专门引入一个 `partial` tag？

**答案**：因为段被截断后，剩余的那部分输入需要被**重新切分和翻译**，而重新切分时希望它「继承」原来的语境（比如仍是某种反查模式的段）。`partial` tag 就是给后续 segmentor（尤其 `affix_segmentor`）的信号：「我是从一次部分选择里拆出来的，请按原 tag 继续处理我」（见 [affix_segmentor.cc:L40-L48](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/affix_segmentor.cc#L40-L48) 里对 `previous_segment.HasTag("partial")` 的特殊处理）。

---

### 4.4 tags 集合与 AddSegment 三规则

#### 4.4.1 概念说明

`tags` 是 `set<string>`，描述「**这段输入是什么类型**」。它是 segmentor 与 translator 之间的**接头暗号**：

- segmentor 切出一段，用 `tags.insert("abc")` 给它涂上颜色；
- translator 看到段上有自己关心的 tag，才决定为这段产出候选（比如 `script_translator` 默认只翻译带 `abc` 的段）；
- filter 也可以根据 tag 决定是否处理某段候选。

librime 里常见的 tag 有：

| tag | 由谁写入 | 含义 |
| --- | --- | --- |
| `abc` | `abc_segmentor` | 字母拼写的片段（拼音等音码） |
| `raw` | `ascii_segmentor` / `fallback_segmentor` | 原样上屏的字符（ASCII 模式或无法识别的兜底） |
| `punct` | `punct_segmentor`（u6-l3） | 标点片段 |
| `phony` | `affix_segmentor` | 「假段」——只作前缀/后缀提示，不参与提交 raw 输入 |
| `xxx_prefix` / `xxx_suffix` | `affix_segmentor` | 反查等模式的前缀/后缀段（`xxx` 是 `tag` 名） |
| `partial` | `Segment::Close()` | 部分选中后拆出的剩余段 |
| 用户自定义 | 配置 `extra_tags` | 方案可为任意 segmentor 追加额外 tag |

一个 Segment 可以**同时有多个 tag**（`tags` 是集合）。例如 `abc_segmentor` 支持读 `abc_segmentor/extra_tags` 配置，给所有 `abc` 段再追加额外 tag（[abc_segmentor.cc:L26-L32](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/abc_segmentor.cc#L26-L32)）。

#### 4.4.2 核心流程

多个 segmentor 对同一段输入表态时，`AddSegment` 用**三条规则**把它们归并：

1. **左对齐规则**：只接受 `segment.start == 当前起点` 的表态，其余拒绝（保证一轮里只切同一段）。
2. **贪心最长规则**：如果新段比已有的长，**覆盖**短的（取信息更多的方案）；如果新段更短，**忽略**。
3. **等长合并规则**：如果新段与已有段等长，**把两段的 tags 取并集**（投票：你说 abc、我说 punct，那就都算）。

这三条规则合起来就是「**长着赢、平者并**」的策略。伪代码：

```
AddSegment(seg):
    if seg.start != 当前起点: return False       # 规则 1
    if 空: push(seg); return True
    last = back()
    if last.end > seg.end:  pass                  # 规则 2：保留更长的 last
    elif last.end < seg.end: last = seg           # 规则 2：用更长的 seg 覆盖
    else: last.tags = last.tags ∪ seg.tags        # 规则 3：等长则合并 tag
    return True
```

#### 4.4.3 源码精读

`AddSegment` 的完整实现把三条规则写得明明白白，注释也点明了每条的用意：

```cpp
// segmentation.cc:L84-L111
bool Segmentation::AddSegment(Segment segment) {
  int start = GetCurrentStartPosition();
  if (segment.start != start) {
    // rule one: in one round, we examine only those segs
    // that are left-aligned to a same position
    return false;
  }

  if (empty()) {
    push_back(segment);
    return true;
  }

  Segment& last = back();
  if (last.end > segment.end) {
    // rule two: always prefer the longer segment...
  } else if (last.end < segment.end) {
    // ...and overwrite the shorter one
    last = segment;
  } else {
    // rule three: with segments equal in length, merge their tags
    set<string> result;
    set_union(last.tags.begin(), last.tags.end(), segment.tags.begin(),
              segment.tags.end(), std::inserter(result, result.begin()));
    last.tags.swap(result);
  }
  return true;
}
```

注意规则三用的是 `std::set_union`（来自 `<algorithm>`），它要求两个集合有序（`set<string>` 天然有序），结果去重后写入新 `result` 再 `swap` 进 `last.tags`。

现在看 segmentor 是怎么「填表」的。最典型的是 `abc_segmentor`：它沿着字母表从起点 `j` 往后扫，停在第一个「既非字母也非分隔符」的字符上，得到区间 `[j, k)`，然后造段、写 tag：

```cpp
// abc_segmentor.cc:L59-L66
if (j < k) {
  Segment segment(j, k);
  segment.tags.insert("abc");
  for (const string& tag : extra_tags_) {
    segment.tags.insert(tag);
  }
  segmentation->AddSegment(segment);
}
```

`fallback_segmentor` 是「兜底」：当前面所有 segmentor 都没切出东西（`GetCurrentSegmentLength() == 0`）时，它把**单个字符**切成一段并标 `raw`，并且因为它是最后被调用的，返回 `false` 终止本轮：

```cpp
// fallback_segmentor.cc:L47-L56
{
  Segment segment(k, k + 1);
  segment.tags.insert("raw");
  segmentation->Forward();
  segmentation->AddSegment(segment);
}
// fallback segmentor should be the last being called, so end this round
return false;
```

`ascii_segmentor` 更激进：在 `ascii_mode` 下，它把**从起点到输入末尾的整段**一次性标成 `raw`，并返回 `false` 直接终结整个切分（因为 ASCII 模式下整段都是原样字符，不需要再切）：

```cpp
// ascii_segmentor.cc:L19-L30
bool AsciiSegmentor::Proceed(Segmentation* segmentation) {
  if (!engine_->context()->get_option("ascii_mode"))
    return true;
  const string& input = segmentation->input();
  size_t j = segmentation->GetCurrentStartPosition();
  if (j < input.length()) {
    Segment segment(j, input.length());
    segment.tags.insert("raw");
    segmentation->AddSegment(segment);
  }
  return false;  // end of segmentation
}
```

注意 segmentor 的返回值语义：`Proceed` 返回 `true` 表示「**继续请下一个 segmentor 发言**」，返回 `false` 表示「**本段已定案，别再问了**」。`Engine::CalculateSegmentation` 据此决定是否 `break` 内层循环（[engine.cc:L180-L183](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L180-L183)）。

把这些 segmentor 的表态代入 `AddSegment` 的三规则，就能解释「为什么 `luna_pinyin` 的 segmentors 列表（[data/minimal/luna_pinyin.schema.yaml](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.schema.yaml) 里 `ascii_segmentor / matcher / abc_segmentor / affix_segmentor@* / punct_segmentor / fallback_segmentor`）能协作切分」：每个 segmentor 只在自己「认得」时表态，`AddSegment` 取最长、并列合并 tag，`fallback` 兜底保证任何字符都不会漏切。

#### 4.4.4 代码实践

**目标**：用 `AddSegment` 三规则，推演一段含「标点」的输入如何被切成多 tag 段（本讲另一主实践：解释 tags 如何由 segmentor 写入）。

**步骤**：

1. 假设输入 `input_ = "ni"`，且 `abc_segmentor` 配置了 `extra_tags: [ reverse ]`（即 `abc_segmentor/extra_tags` 列表里有一项 `reverse`，见 [abc_segmentor.cc:L26-L32](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/abc_segmentor.cc#L26-L32)）。
2. 推演 `abc_segmentor::Proceed`：扫到 `[0,2)` 都是字母，造 `Segment(0,2)`，`tags = {"abc", "reverse"}`，调 `AddSegment`。
3. 由于这是第一段，`AddSegment` 走 `empty()` 分支直接 `push_back`。
4. 假设还有另一个 segmentor（虚构，仅用于演示规则三）也表态 `Segment(0,2)` 但 `tags = {"punct"}`。代入规则三：`end` 相等（都是 2），`set_union` 把 `{"abc","reverse"}` 与 `{"punct"}` 合并成 `{"abc","punct","reverse"}`。
5. 想象 translator 配置里有一条「只翻译带 `reverse` tag 的段」的反查翻译器——它正是靠 `segment.HasTag("reverse")` 找到这段并产出候选。

**需要观察的现象**：`extra_tags` 机制让同一段物理输入能**同时被多种翻译器关注**，这是 librime 实现「一段输入、多种解读」（如同时正向查词 + 反查）的关键。

**预期结果**：最终该段 `tags = {"abc", "reverse"}`（若再加上述虚构 punct 表态则为 `{"abc","punct","reverse"}`）。理解了「tag 不是互斥的单选，而是可叠加的集合」。

> 说明：步骤 4 的「另一个 segmentor 表态同长段」是为演示规则三而构造的情形；真实 `luna_pinyin` 默认配置下同一段通常只有一个 segmentor 表态，规则三更多在 `abc_segmentor` 与 `punct_segmentor` 对某些符号（既算字母相关又算标点）的边界情形或多个 affix segmentor 重叠时生效。

#### 4.4.5 小练习与答案

**练习 1**：`AddSegment` 的规则一「左对齐」是为了防止什么？如果不检查会怎样？

**答案**：防止某个 segmentor 越界表态「下一段或更远的位置」。一轮内层循环的目标是**切当前位置的这一段**，所有 segmentor 都应针对同一个起点 `start` 表态。若不检查，某个 segmentor 可能提交一个起点错位的段，破坏「首尾相接」不变式，导致后续段位置混乱、`HasFinishedSegmentation` 判断失真。规则一把这种越界表态直接拒掉（返回 `false`）。

**练习 2**：`fallback_segmentor` 在写新 raw 段之前先调了 `Forward()`（[fallback_segmentor.cc:L52](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/fallback_segmentor.cc#L52)），而 `abc_segmentor` 写段之前没调 `Forward()`。为什么？

**答案**：因为进入一轮时，`Segmentation` 的最后一个元素通常是一个 `start==end` 的「空脚手架段」（由上一轮的 `Forward` 留下，或初始状态）。`abc_segmentor` 直接用 `AddSegment(Segment(j,k))` 把这个空段**覆盖/填充**成有内容的段（走规则二覆盖或规则三合并）。而 `fallback_segmentor` 处理的是「前面都没切出来」的情形，它需要先把那个空段 `pop_back` 掉（见 [fallback_segmentor.cc:L29-L32](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/fallback_segmentor.cc#L29-L32)），再 `Forward()` 腾出新格，最后 `AddSegment`。两者面对的「当前末段状态」不同，所以调用序列不同。

**练习 3**：如果一个 translator 想「**只处理标点段**」，它应该检查 Segment 的什么？

**答案**：检查 `segment.HasTag("punct")`（或 `HasAnyTagIn({"punct", ...})`）。tag 正是 segmentor 与 translator 之间的契约：segmentor 负责涂色，translator 负责按色认领。这也是为什么 `abc_segmentor` 支持 `extra_tags`——让方案能灵活地把额外 tag 涂到段上，从而把段导向非默认的 translator。

---

### 4.5 Forward / Trim：推进与维护切分边界

#### 4.5.1 概念说明

`Forward` 和 `Trim` 是维持 `Segmentation` 不变式的两个「**搬运工**」：

- `Forward`：在当前段定案后，**追加一个空的脚手架段**（`start==end==当前段末尾`），把「当前填写位置」推进到下一段。
- `Trim`：**撤掉尾部多余的空段**，让 `Segmentation` 以一个有内容的段收尾。

没有 `Forward`，所有 segmentor 会挤在第一段反复表态；没有 `Trim`，调试输出和后续遍历会多出一个无意义的空段。

#### 4.5.2 核心流程

```
一轮切分结束（最后一个表态的 segmentor 返回 false，或内层 for 跑完）
   │
   ├─ 引擎检查：end 有没有推进？没推进 → break（切不动）
   ├─ 引擎检查：还没切完？  → segments->Forward()   # 腾出下一段空格
   └─ 回到外层 while 顶部

外层 while 结束（HasFinishedSegmentation 为真，或切不动了）
   │
   ├─ Trim()   # 撤掉尾部空段
   └─ 若末段已 kSelected 以上 → Forward()  # 给已确认合成末尾留个空段（见下）
```

#### 4.5.3 源码精读

`Forward` 的实现极简——若当前已有非空段，就在末尾追加一个 `start==end` 的空段：

```cpp
// segmentation.cc:L113-L120
// finalize a round
bool Segmentation::Forward() {
  if (empty() || back().start == back().end)
    return false;
  // initialize an empty segment for the next round
  push_back(Segment(back().end, back().end));
  return true;
}
```

两个守卫：`empty()` 时没法推进；`back().start == back().end`（末尾已经是空段）时也不推进——避免连续 `Forward` 产生一串空段。新段的 `start` 和 `end` 都等于上一段的 `end`，完美维持「首尾相接」不变式。

`Trim` 是 `Forward` 的逆操作，撤掉尾部那个空脚手架：

```cpp
// segmentation.cc:L122-L129
// remove empty trailing segment
bool Segmentation::Trim() {
  if (!empty() && back().start == back().end) {
    pop_back();
    return true;
  }
  return false;
}
```

把它们放进 `Engine::CalculateSegmentation` 的完整驱动里看，就能看清「谁在何时调 Forward/Trim」：

```cpp
// engine.cc:L171-L201
void ConcreteEngine::CalculateSegmentation(Segmentation* segments) {
  ...
  while (!segments->HasFinishedSegmentation()) {
    size_t start_pos = segments->GetCurrentStartPosition();
    size_t end_pos = segments->GetCurrentEndPosition();
    // recognize a segment by calling the segmentors in turn
    for (auto& segmentor : segmentors_) {
      if (!segmentor->Proceed(segments))
        break;
    }
    // no advancement
    if (start_pos == segments->GetCurrentEndPosition())
      break;
    // only one segment is allowed past caret pos, which is the segment
    // immediately after the caret.
    if (start_pos >= context_->caret_pos())
      break;
    // move onto the next segment...
    if (!segments->HasFinishedSegmentation())
      segments->Forward();
  }
  // start an empty segment only at the end of a confirmed composition.
  if (!segments->empty() && !segments->back().HasTag("placeholder"))
    segments->Trim();
  if (!segments->empty() && segments->back().status >= Segment::kSelected)
    segments->Forward();
}
```

几个关键点：

- 内层 `for` 调各 segmentor，遇 `false` 即 `break`（本段定案）。
- **防死循环**：若一轮下来 `end_pos` 没推进（`start_pos == GetCurrentEndPosition()`），直接 `break`，避免空转。
- **光标约束**：只允许「光标之后的那一段」被翻译（`start_pos >= caret_pos` 时停），避免提前翻译用户还没敲完的部分。
- **收尾 Trim**：把尾部空段撤掉。
- **末尾再 Forward**：若末段已 `kSelected` 以上（已确认的合成末尾），又 `Forward()` 留一个空段——这是给「在已确认内容之后再插一段」预留接口（注释 `start an empty segment only at the end of a confirmed composition`）。

至此，`Segmentation` 从空到满的完整生命周期就闭环了：`Reset` 增量清理 → `CalculateSegmentation` 循环（`AddSegment` 填段 + `Forward` 推进）→ `Trim` 收尾。

#### 4.5.4 代码实践

**目标**：在源码里「单步」追踪一次切分，验证 `Forward`/`Trim` 的调用时机。

**步骤**：

1. 打开 [segmentation.cc:L113-L129](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/segmentation.cc#L113-L129) 与 [engine.cc:L171-L201](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L171-L201)。
2. 假设输入 `"ab"`、segmentors = `[abc_segmentor, fallback_segmentor]`、光标在末尾（`caret_pos == 2`）。
3. 第 1 轮：`start_pos=0`。`abc_segmentor` 表态 `Segment(0,2){abc}`，`AddSegment` 走 `empty()` 分支压入。返回 `true`，继续。`fallback_segmentor` 看到 `GetCurrentSegmentLength()==2>0`，直接 `return false`（[fallback_segmentor.cc:L19-L20](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/fallback_segmentor.cc#L19-L20)），内层 `break`。
4. `end_pos` 从 0 推进到 2，不触发防死循环；`start_pos(0) >= caret_pos(2)`? 否，继续；`HasFinishedSegmentation`? `back().end==2==input_.length()`，为真，**不调** `Forward`，退出 while。
5. 收尾：`Trim` 发现末段 `start(0) != end(2)`，不是空段，不撤；末段 `status` 是 `kVoid`（还没翻译），`< kSelected`，不 `Forward`。
6. 最终 `Segmentation` 只有一个段 `[0,2){abc}`。

**需要观察的现象**：`Forward` 并不是每轮都调——只有「本段切完且整串还没切完」时才调。`Trim` 只撤真正的空段。这两者的守卫条件保证了 `Segmentation` 永远干净。

**预期结果**：手工追踪得到的最终状态是 `[ab|0,2{abc}]`，与 `operator<<` 的格式一致。

> 「待本地验证」：可在 `Forward`/`Trim`/`AddSegment` 的 `DLOG` 处观察真实运行日志；若未开日志，本推演已依据源码逻辑可复现。

#### 4.5.5 小练习与答案

**练习 1**：`Forward` 的守卫 `back().start == back().end` 返回 `false` 是为了什么？

**答案**：为了避免「末尾已经是空脚手架段时，再追加一个空段」——那会产生两个相邻的空段，既破坏调试输出的可读性，也让下一轮 segmentor 困惑（`GetCurrentStartPosition` 仍指向同一位置，但多了一层无意义结构）。这个守卫保证「至多只有一个尾部空段」。

**练习 2**：`CalculateSegmentation` 末尾的 `if (back().status >= kSelected) Forward();` 为什么只在「已确认合成末尾」时才追加空段？

**答案**：因为只有当末段已经被用户选定（`kSelected` 以上），才存在「在其后再插入新输入、形成新段」的现实需求——用户可能在已选内容之后继续敲键。此时预留一个空段作为「下一段的起点」。若末段还没选（`kVoid`/`kGuess`），用户继续敲键只会延长当前段，不需要新段，所以不 `Forward`。注释 `start an empty segment only at the end of a confirmed composition` 正是此意。

**练习 3**：假如某个 segmentor 在 `Proceed` 里忘记返回 `false`，且没有任何 segmentor 能切动当前位置，会发生什么？引擎会卡死吗？

**答案**：不会卡死。`CalculateSegmentation` 有防死循环守卫：`if (start_pos == segments->GetCurrentEndPosition()) break;`——只要一轮下来 `end` 没推进，外层 while 立即 `break`。所以即便 segmentor 配置有误（都返回 `true` 但没人切），引擎也只是提前结束切分、当前段为空，不会无限循环。这是一个重要的健壮性设计。

---

## 5. 综合实践

**任务**：以一段稍复杂的输入 `","`（逗号）或 `"xian"`（典型拼音歧义串）为样本，**端到端**追踪它在 `Segmentation` 层面的完整演化，把本讲四个模块（容器、字段、状态机、tags）串成一张「输入 → 切分 → 状态」全图。

**步骤**：

1. **选定样本**：选 `"xian"`（拼音里既可读 `xi'an` 也可读 `xian`，是切分歧义的经典例子）。注意：本讲只关心 **segmentor 层** 的切分，**音节内部**的歧义由 `Syllabifier`（u7-l3）处理，segmentor 层通常把整串 `xian` 切成一个 `abc` 段。
2. **画初始状态**：`Segmentation` 为空，`input_ = "xian"`，segmentors = luna_pinyin 的列表。
3. **推第 1 轮**（起点 0）：
   - `ascii_segmentor`：非 `ascii_mode`，返回 `true` 跳过。
   - `matcher`：一般不认字母，跳过。
   - `abc_segmentor`：沿默认字母表（注意默认是反向串 `"zyxwvutsrqponmlkjihgfedcba"`，[abc_segmentor.cc:L13](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/abc_segmentor.cc#L13)，但 `find` 对单字符不受顺序影响）扫，`x/i/a/n` 都是字母，扫到 `k=4`，表态 `Segment(0,4){abc}`，`AddSegment` 压入。
   - `affix_segmentor@*`：当前段没有匹配的前缀，跳过。
   - `punct_segmentor`：不是标点，跳过。
   - `fallback_segmentor`：`GetCurrentSegmentLength()==4>0`，直接 `return false`。
   - 结果：`[xian|0,4{abc}]`。
4. **检查循环条件**：`HasFinishedSegmentation`? `back().end==4==input_.length()`，为真，退出 while。`Trim`：末段非空，不动。末段 `status==kVoid < kSelected`，不 `Forward`。
5. **标注状态机**：当前段 `status=kVoid`（还没翻译）。在图上画出它即将经历的迁移：translator 挂 menu 后 → `kGuess`；用户选候选后 → `kSelected`；提交后 → `kConfirmed`。
6. **延伸思考**：若用户此时把光标移到位置 2（`xi|an`），`Engine::Compose` 会怎样？`comp.Reset("xian")` 发现 `diff_pos` 仍在 4（输入串没变），但 `caret_pos(2) != GetConfirmedPosition()`，所以不会触发「光标处重切」的分支（[engine.cc:L161-L165](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L161-L165)）；不过 `caret_pos` 影响的是「翻译到哪」与 preedit 显示，segmentor 层切分仍是整段 `abc`。

**进阶**：

- 把样本换成 `"ni,"`（拼音 + 标点），重做步骤 3-5，验证你会得到两段 `[0,2{abc}|2,3{punct}]`，并解释为什么 `punct_segmentor` 在第二轮才表态（因为它在第一轮面对的是字母，认不出）。
- 思考：如果方案把 `fallback_segmentor` 从 segmentors 列表里删掉，输入一个既非字母也非标点的怪字符会怎样？（提示：防死循环守卫会让 `CalculateSegmentation` 提前 `break`，该字符不会被切成段，`HasFinishedSegmentation` 为假但循环已退出——这就是 `fallback` 作为兜底的价值。）

**预期产出**：一张包含「输入串 → 分段区间 → tags → status」四列的表，外加一张四态状态机迁移图（来自 4.3.4）。这张图表应当能回答：「为什么我敲 `xian` 时，引擎内部会出现一个带 `abc` tag、`status=kVoid` 的 `[0,4)` 段，而它随后会怎样成熟？」这是连接 u3-l1（Context）与 u3-l3（Composition/Translation/Candidate）的桥梁。

## 6. 本讲小结

- `Segmentation` 就是 `public vector<Segment>` 外加一份 `input_` 与一组维护方法，用一串首尾相接的 `Segment` 覆盖整段输入；不变式是「第 i 段的 `end` == 第 i+1 段的 `start`」。
- `Reset(new_input)` 采用**增量策略**：只算与新输入的第一个不同位置 `diff_pos`，丢弃受影响的尾部段，保留已确认的前缀，避免每次从零重切。
- `Segment` 用 `[start, end)` 半开区间定位、`tags`（`set<string>`）涂色、`status`（四态）记成熟度，另有 `menu`/`selected_index`/`prompt` 承载候选与提示；`Clear()` 重置候选但保留位置。
- `Status` 四态 `kVoid/kGuess/kSelected/kConfirmed` 按**成熟度递增**数值化，所以源码大量用 `status >= kSelected` 这类区间判断；`Reopen` 让已选段可回退编辑、`Close` 处理部分选中拆段（引入 `partial` tag）。
- `tags` 是 segmentor 与 translator 的**接头暗号**：`abc`（字母拼写）/`raw`（原样上屏）/`punct`（标点）/`phony`（假段）/`xxx_prefix`/`xxx_suffix`（前后缀）/`partial`（部分选中拆出）各自由对应 segmentor 或 `Close` 写入；一个段可同时有多个 tag。
- `AddSegment` 三规则——**左对齐**（只切当前位置）、**贪心最长**（长段覆盖短段）、**等长合并 tag**（`set_union`）——把多个 segmentor 的表态归并成一段；`Forward` 腾新格、`Trim` 撤空格，`CalculateSegmentation` 用「end 未推进即 break」的守卫防止死循环。

## 7. 下一步学习建议

- **u3-l3 Composition、Translation 与 Candidate**：本讲把 `Segment` 的 `menu` 字段当黑盒，下一讲拆开它——`Composition` 如何继承 `Segmentation` 并组织候选、`Translation` 如何以迭代器形式被拉取、`Candidate` 的 `text/comment/preedit/start/end` 字段结构，以及 `Segment::menu` 是怎么被填上的。
- **u3-l4 Menu 与分页**：进一步看 `Menu` 如何把 `Translation` + `Filter` 链串成惰性候选流，并按 `page_size` 分页产出（呼应本讲 `selected_index` 与 `page_size` 的来源）。
- **u6-l1 引擎流水线总览**：把本讲的 `CalculateSegmentation` 放回 `ProcessKey` 主线，看 Processor → Segmentor → Translator → Filter 如何在一次按键里协同。
- **u6-l3 Segmentor 组件族**：本讲只取了 `abc`/`fallback`/`ascii`/`affix` 几个 segmentor 的「写 tag」片段，那一讲会完整讲解各 segmentor 的切分逻辑与 `tag` 体系。
- **配套阅读**：可对照 [data/minimal/luna_pinyin.schema.yaml](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.schema.yaml) 的 `engine/segmentors` 列表，把本讲提到的每个 segmentor 名字与它写入的 tag 一一对应，巩固「配置 → 组件 → tag」的映射。
