# Composition、Translation 与 Candidate

## 1. 本讲目标

在 u3-l1 中我们知道了 `Context::composition_` 是「候选组织」的黑盒，在 u3-l2 中我们拆开了它的第一层——`Segmentation` 与 `Segment`，明白了输入串如何被切成首尾相接的段。但还有一个关键问题没有回答：**段里的候选词到底从哪里来？它们如何被排序、去重、最终拼成上屏文字？**

本讲拆开这个黑盒的最里一层，回答三个问题：

1. 一条候选项（`Candidate`）在内存里长什么样？
2. 候选词是如何被「按需逐个拉取」的（`Translation` 迭代器模式）？
3. 多个翻译器的候选如何被归并、过滤，并挂回 `Segment` 上（`Composition` + `Menu`）？

学完后，你应当能画出这样一条完整数据流：

```
Translator::Query()  ──返回──►  Translation（候选流）
                                        │
                                  Menu.AddTranslation()（多个）
                                        ▼
                              MergedTranslation（归并）/ Filter（包装）
                                        │
                              Segment.menu = menu
                                        ▼
                        Composition.GetCommitText()（拼提交文本）
```

## 2. 前置知识

在进入源码前，先确认几个本讲反复用到的概念。

### 2.1 智能指针别名（来自 `common.h`）

librime 在 `common.h` 里给标准库智能指针起了一组极短的别名，本讲的源码里到处都是，必须先记住：

| 别名 | 含义 | 出处 |
|---|---|---|
| `an<T>` | `std::shared_ptr<T>`（共享所有权指针） | [common.h:60](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/common.h#L60) |
| `of<T>` | 就是 `an<T>`，习惯用作容器元素类型 | [common.h:62](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/common.h#L62) |
| `weak<T>` | `std::weak_ptr<T>` | [common.h:64](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/common.h#L64) |
| `New<T>(args...)` | 等价 `std::make_shared<T>(args...)` | [common.h:77](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/common.h#L77) |
| `As<X>(ptr)` | 向下转型（`dynamic_pointer_cast`） | [common.h:67](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/common.h#L67) |
| `Is<X>(ptr)` | 判断指针是否指向 `X` 类型 | [common.h:72](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/common.h#L72) |

所以看到 `an<Candidate>` 读作「指向 Candidate 的 shared_ptr」，看到 `New<SimpleCandidate>(...)` 读作「构造一个 SimpleCandidate 的 shared_ptr」。

### 2.2 Segmentation / Segment（来自 u3-l2）

回忆 u3-l2 的结论：`Segmentation` 是一串首尾相接的 `Segment`，每个 `Segment` 用 `[start, end)` 半开区间覆盖一段输入，并带 `tags`、`status`（四态状态机 `kVoid/kGuess/kSelected/kConfirmed`）、以及一个 `menu` 字段（指向 `Menu`）。本讲要解答的正是这个 `menu` 字段里装的是什么。

### 2.3 迭代器 / 生成器的直觉

如果你写过 Python 的 `generator` 或用过 Java 的 `Iterator`，那么本讲的 `Translation` 你会倍感亲切：它不一次性返回全部候选，而是「你问一次我给一个」，这在候选词可能成千上万（一个大词典）时能省下大量计算和内存——只有真正要显示的那一页候选才会被求值。这种「按需拉取」的模式也叫 **惰性求值（lazy evaluation）** 或 **拉模型（pull model）**。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| [src/rime/candidate.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/candidate.h) | 定义候选项抽象基类 `Candidate` 及 `SimpleCandidate`/`ShadowCandidate`/`UniquifiedCandidate` 三个常用子类。**最底层的数据模型。** |
| [src/rime/candidate.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/candidate.cc) | `Candidate::compare`（排序规则）与 `GetGenuineCandidate(s)`（穿透包装取真实候选）。 |
| [src/rime/translation.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/translation.h) | 定义候选流抽象 `Translation` 及 `UniqueTranslation`/`FifoTranslation`/`UnionTranslation`/`MergedTranslation`/`CacheTranslation`/`DistinctTranslation`/`PrefetchTranslation` 一族子类。**候选的迭代器抽象。** |
| [src/rime/translation.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/translation.cc) | 上述子类的实现，重点是 `UnionTranslation`/`MergedTranslation`/`DistinctTranslation` 的合并去重逻辑。 |
| [src/rime/composition.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/composition.h) | `Composition` 继承 `Segmentation`，提供把各段选中候选拼成提交/预编辑文本的方法。**最上层的组织者。** |
| [src/rime/composition.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/composition.cc) | `GetCommitText`/`GetPreedit`/`GetScriptText`/`GetDebugText` 的实现。 |
| [src/rime/menu.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/menu.h) / [menu.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/menu.cc) | `Menu` 把多个 `Translation` 归并、再串上 `Filter`，按分页产出候选。是 `Translation` 与 `Composition` 之间的桥梁。 |
| [src/rime/engine.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc) | `ConcreteEngine::TranslateSegments` 把 translator 产出的 `Translation` 收集进 `Menu` 再挂到 `Segment`，是整条数据流的入口。 |
| [sample/src/trivial_translator.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/sample/src/trivial_translator.cc) | 官方示例插件里的最小翻译器，演示 `SimpleCandidate` + `UniqueTranslation` 的标准用法。 |

本讲的三个最小模块按 **自底向上** 顺序讲解：先讲最底层的 `Candidate`（数据模型），再讲产出它的 `Translation`（迭代器），最后讲组织它们的 `Composition`（聚合层）。

---

## 4.1 Candidate：候选项的数据模型

### 4.1.1 概念说明

当用户输入 `ni hao`，候选窗里出现的每一行（「你好」「你好吗」「尼豪」……）在内存里都是一个 `Candidate` 对象。`Candidate` 是个**抽象基类**，它把「一条候选」抽象成五个关键属性：

| 属性 | 含义 | 是否必须 |
|---|---|---|
| `text()` | 真正要提交（上屏）的文字，如「你好」 | **纯虚**，子类必须实现 |
| `comment()` | 提示信息，如拼音 `ni hao`、词频标记 | 可选，默认返回空串 |
| `preedit()` | 显示在预编辑区（preedit）的文字，默认可与 `text` 不同 | 可选，默认空串 |
| `start()` / `end()` | 这条候选在原始输入串中覆盖的范围 `[start, end)` | 构造时给定 |
| `type()` | 字符串标签，标识「这条候选来自哪个翻译器」，学习/统计阶段用 | 构造时给定 |
| `quality()` | 质量分，用于多条候选间排序 | 默认 0 |

为什么 `text()` 是纯虚、而 `comment()`/`preedit()` 不是？因为 **任何候选都必须有上屏文字**（否则它没意义），但提示和预编辑文本可有可无。同时，把 `text()`/`comment()`/`preedit()` 都设为虚函数，让子类可以**按需决定文字从哪里来**——是直接存一个字符串（`SimpleCandidate`），还是去问被包装的另一个候选（`ShadowCandidate`）。这是一种典型的**策略模式**。

### 4.1.2 核心流程

`Candidate` 体系有两个核心流程。

**流程一：排序规则 `compare`**

两条候选谁该排在前面？`Candidate::compare` 给出确定性规则，按优先级依次比较：

```
1. start 小的在前（靠近段首的候选优先）
2. 若 start 相同，end 大的在前（覆盖更长输入的候选优先）
3. 若区间相同，quality 高的在前
4. 全相等则视为平局（返回 0）
```

这个规则被 `Translation::Compare` 调用（见 4.2），决定了多翻译器候选的归并顺序。

**流程二：穿透包装取真实候选 `GetGenuineCandidate(s)`**

`Candidate` 有两个「包装类」子类（`ShadowCandidate`、`UniquifiedCandidate`），它们本身不存文字，而是包装别的候选。当某个组件（比如提交逻辑）需要拿到「最原始的那条候选」时，就要穿透这些包装。`GetGenuineCandidate` 做的就是：若是 `UniquifiedCandidate` 取其第一个元素，若是 `ShadowCandidate` 取其 `item()`，否则原样返回。

### 4.1.3 源码精读

先看抽象基类 [`Candidate`](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/candidate.h#L14-L49)：

```cpp
// candidate.h:14-49
class Candidate {
 public:
  Candidate(const string& type, size_t start, size_t end, double quality = 0.)
      : type_(type), start_(start), end_(end), quality_(quality) {}
  // candidate text to commit
  virtual const string& text() const = 0;        // 纯虚：必须实现
  virtual string comment() const { return string(); }   // 默认空
  virtual string preedit() const { return string(); }   // 默认空
  ...
 private:
  string type_;
  size_t start_ = 0;
  size_t end_ = 0;
  double quality_ = 0.;
};
```

注意构造函数只接收 `type/start/end/quality` 四个「定位与排序」字段，**不接收文字**——文字交给子类用自己的方式提供。

再看三个具体子类，它们体现了「文字从哪里来」的三种策略：

**`SimpleCandidate`** —— 自己存文字（[candidate.h:56-82](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/candidate.h#L56-L82)）：

```cpp
class SimpleCandidate : public Candidate {
  ...
  const string& text() const { return text_; }      // 直接返回成员
  string comment() const { return comment_; }
  string preedit() const { return preedit_; }
 protected:
  string text_, comment_, preedit_;   // 三个字符串成员
};
```

这是最常见的形式，`trivial_translator.cc:44` 就是用它造候选。

**`ShadowCandidate`** —— 借别人的文字（[candidate.h:84-110](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/candidate.h#L84-L110)）：

```cpp
class ShadowCandidate : public Candidate {
  ...
  const string& text() const {
    return text_.empty() ? item_->text() : text_;   // 没覆写就借底层
  }
  string comment() const {
    return inherit_comment_ && comment_.empty() ? item_->comment() : comment_;
  }
  string preedit() const { return item_->preedit(); }   // 永远借底层
 protected:
  string text_, comment_;
  an<Candidate> item_;     // 被包装的「真身」
  bool inherit_comment_;
};
```

`ShadowCandidate` 是 **繁简转换 filter（simplifier）** 的主力工具：它不重新造一条候选，而是给已有候选套一个「影子」，只改 `text`（比如「简」→「簡」），其余信息（区间、preedit）原样继承。`inherit_comment_` 控制提示文字是否也继承。

**`UniquifiedCandidate`** —— 合并文字相同的候选（[candidate.h:112-146](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/candidate.h#L112-L146)）：

```cpp
class UniquifiedCandidate : public Candidate {
  ...
  void Append(an<Candidate> item) {
    items_.push_back(item);
    if (quality() < item->quality())
      set_quality(item->quality());   // 取最大质量
  }
  const CandidateList& items() const { return items_; }
 protected:
  string text_, comment_;
  CandidateList items_;    // 同义候选列表
};
```

当多个翻译器（或同一翻译器的多条路径）给出文字完全相同的候选时，`uniquifier` filter 会用 `UniquifiedCandidate` 把它们合并成一条显示，避免候选窗里出现重复行；原始候选保留在 `items_` 里，提交时仍可穿透取回。`quality()` 取成员中的最大值，保证合并后排序不劣化。

最后看排序规则 [`Candidate::compare`](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/candidate.cc#L34-L49)：

```cpp
// candidate.cc:34-49
int Candidate::compare(const Candidate& other) {
  int k = start_ - other.start_;        // 1. 段首优先
  if (k != 0) return k;
  k = end_ - other.end_;
  if (k != 0) return -k;                 // 2. 更长优先（注意取负）
  double qdiff = quality_ - other.quality_;
  if (qdiff != 0.) return (qdiff > 0.) ? -1 : 1;   // 3. 质量优先
  return 0;                              // 平局
}
```

第 38 行 `return -k` 是个容易看错的细节：`end_ - other.end_` 为正表示「我更长」，而 `compare` 约定 **负值表示「我应排在前面」**，所以对长度差取负号，让更长的候选返回负值排到前面。

### 4.1.4 代码实践

**实践目标**：通过官方示例插件 `trivial_translator` 看 `SimpleCandidate` 的最小用法，并亲手修改它的提示文字。

**操作步骤**：

1. 打开 [sample/src/trivial_translator.cc:34-47](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/sample/src/trivial_translator.cc#L34-L47)，定位 `Query` 方法：

   ```cpp
   auto candidate = New<SimpleCandidate>("trivial", segment.start, segment.end,
                                         output, ":-)");
   return New<UniqueTranslation>(candidate);
   ```

   这里把 `type` 设为 `"trivial"`、`comment` 设为 `":-)"`，并用 `UniqueTranslation`（4.2 会讲）包成单候选流。

2. 阅读构造函数（[trivial_translator.cc:16-32](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/sample/src/trivial_translator.cc#L16-L32)），理解它把 `yi→一`、`er→二` 这样的映射写死在一个 `map` 里。

3. 把第 45 行的 `":-%29"` 改成你自己的提示串（例如拼音 `output` 本身），重新编译 sample 插件。

**需要观察的现象**：在装配了 sample 方案的 `rime_api_console` 里输入 `yi`，候选窗第一条应是「一」，其后的提示文字会从 `:-)` 变成你新写的串。

**预期结果**：候选文字不变，仅 `comment()` 变化——说明 `SimpleCandidate` 的 `text` 与 `comment` 是相互独立的两个成员。

> 说明：本实践需要先按 u1-l2 编译 librime、再按 u9-l6 编译 sample 插件并配置 sample 方案。若暂未搭建环境，可只做源码阅读部分，标注「待本地验证」运行现象。

### 4.1.5 小练习与答案

**练习 1**：为什么 `Candidate::text()` 是纯虚函数，而 `comment()`/`preedit()` 不是？

**答案**：因为任何候选都必须有可提交的文字，没有文字的候选没有存在意义，所以强制子类实现 `text()`；而提示和预编辑文本是可选的装饰信息，给一个返回空串的默认实现更方便，子类按需覆写即可。

**练习 2**：`Candidate::compare` 里比较 `end` 时为什么对差值取负号（`return -k`）？

**答案**：`compare` 的约定是「返回负值表示本候选应排在前面」。`end_ - other.end_` 为正代表「我覆盖的输入更长」，而我们希望更长的候选排前面，所以对差值取负，让「更长」对应到「负值→排前」。

**练习 3**：`ShadowCandidate` 与 `UniquifiedCandidate` 分别解决什么问题？

**答案**：`ShadowCandidate` 解决「给已有候选换一件文字外衣」（如繁简转换）而不丢失其区间与 preedit；`UniquifiedCandidate` 解决「把文字相同的多个候选合并成一条显示」以去重，同时保留底层原始候选供穿透取回。

---

## 4.2 Translation：候选拉取迭代器

### 4.2.1 概念说明

`Candidate` 描述「一条候选」，但一个翻译器对同一段输入往往能给出**很多条**候选（输入 `yi`，词典里可能有「一、已、以、意……」几十条）。如果每次按键都把所有候选一次性算出来装进 `vector`，既慢又费内存——毕竟用户通常只看第一页。

`Translation` 解决的就是这个问题：它把「候选集合」抽象成一个**惰性的迭代器**（注释里叫 "generator of candidates"）。你不预先知道它总共有多少候选，只能：

- `Peek()`：偷看当前指向的那条候选（不消费它）；
- `Next()`：向后走一步，返回是否还没到末尾；
- `exhausted()`：是否已经耗尽（没候选了）。

这套 `Peek + Next` 的接口和 C++ STL 的输入迭代器、Python 的 `generator`、Java 的 `Iterator` 是同一类思想。它的价值在于：**只有真正被 `Peek` 过的候选才会被求值**，分页显示时算到哪页才查到哪页。

### 4.2.2 核心流程

`Translation` 的状态机很简单：

```
[未耗尽 exhausted=false]
        │
        │ Next()  ──► 前进一步
        │              │
        │              ├── 还有候选 ──► 仍 [未耗尽]
        │              └── 到末尾   ──► set_exhausted(true) ──► [耗尽]
        ▼
     Peek() 返回当前候选（耗尽时返回 nullptr）
```

真正有趣的是它的一族**组合子类**——很多子类本身又包装别的 `Translation`，构成装饰器/组合模式。按用途分三类：

**A. 基本容器型**

| 子类 | 行为 |
|---|---|
| `UniqueTranslation` | 只有一条候选；`Next()` 一次即耗尽 |
| `FifoTranslation` | 先进先出队列；`Append()` 追加候选，按入队顺序产出 |

**B. 多流合并型（本讲重点）**

| 子类 | 行为 |
|---|---|
| `UnionTranslation` | **串接**：把多个流首尾相接，前一个耗尽才轮到下一个；保序、不去重 |
| `MergedTranslation` | **归并**：像归并排序那样，用 `Compare` 实时选出当前最优，让多个流的候选交叉排序 |

**C. 装饰器型（包装单个流）**

| 子类 | 行为 |
|---|---|
| `CacheTranslation` | 缓存 `Peek()` 结果，避免底层重复计算 |
| `DistinctTranslation` | 在 `CacheTranslation` 基础上跳过 `text` 重复的候选（去重） |
| `PrefetchTranslation` | 带一个小队列，子类可 `Replenish()` 预填候选 |

**关于 `Compare` 的返回值约定**（这是理解 `MergedTranslation` 的钥匙，见 [translation.h:28-30](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/translation.h#L28-L30)）：

- 返回**负值或零**：本 Translation 当前候选更优，**应该由本流先提供**候选；
- 返回**正值**：本流更劣，**让位**给其他流。

`MergedTranslation` 的 `Elect()` 正是用这个约定，在每一步从所有子流里选出「Compare ≤ 0」的那个作为当前输出，等价于一个 **k 路归并**。

### 4.2.3 源码精读

先看抽象基类 [`Translation`](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/translation.h#L17-L39)：

```cpp
// translation.h:17-39
class Translation {
 public:
  virtual bool Next() = 0;          // 前进一步，返回是否未耗尽
  virtual an<Candidate> Peek() = 0; // 当前候选（不消费）
  virtual int Compare(an<Translation> other, const CandidateList& candidates);
  bool exhausted() const { return exhausted_; }
 protected:
  void set_exhausted(bool exhausted) { exhausted_ = exhausted_; }
 private:
  bool exhausted_ = false;
};
```

`Next()` 与 `Peek()` 都是纯虚——每个子类自己决定「怎么前进、当前是谁」。`exhausted_` 是受保护的标志，子类通过 `set_exhausted()` 翻它。

`Compare` 的默认实现在 [translation.cc:12-23](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/translation.cc#L12-L23)，逻辑是：把双方的当前候选 `Peek` 出来，委托给 `Candidate::compare`（4.1.3 讲过）比较。注意 `Compare` 对「对方为空/已耗尽」「自己已耗尽」都做了短路处理，确保归并时不会访问空候选。

接下来看三个重点子类。

**`UnionTranslation`：串接（保序、不去重）** —— [translation.cc:65-90](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/translation.cc#L65-L90)

```cpp
bool UnionTranslation::Next() {
  translations_.front()->Next();                 // 让队首流前进
  if (translations_.front()->exhausted()) {
    translations_.pop_front();                   // 队首耗尽则丢弃
    if (translations_.empty()) set_exhausted(true);
  }
  return true;
}
an<Candidate> UnionTranslation::Peek() {
  return translations_.front()->Peek();          // 永远取队首的当前候选
}
```

内部用一个 `list<of<Translation>>`，`Peek/Next` 永远只作用在队首流；队首耗尽就 `pop_front` 换下一个。效果是 `[A1,A2,A3] + [B1,B2] → A1,A2,A3,B1,B2`，**严格保序、不会交叉、不去重**。它适合「先 A 后 B」的明确优先级场景。

**`MergedTranslation`：归并（交叉排序）** —— [translation.cc:106-152](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/translation.cc#L106-L152)

```cpp
void MergedTranslation::Elect() {
  size_t k = 0;
  for (; k < translations_.size(); ++k) {
    const auto& current = translations_[k];
    const auto& next = k+1 < translations_.size() ? translations_[k+1] : nullptr;
    if (current->Compare(next, previous_candidates_) <= 0) {  // 我不劣于后继
      if (current->exhausted()) { translations_.erase(...); k = 0; continue; }
      break;   // 找到赢家
    }
  }
  elected_ = k;   // 记录当选流的下标
  ...
}
```

`Elect` 在每次 `Next` 之后重新选一次「当前最优流」`elected_`，`Peek` 返回 `translations_[elected_]->Peek()`。其行为等价于 **按 `Candidate::compare` 排序的 k 路归并**：每一步都让所有子流中当前候选最优的那一个出头。这正是 `Menu` 用来混合多个翻译器候选的方式——拼音方案里 `script_translator`（词典词）与 `punctuator`（标点）的候选会按质量交叉出现，而不是一个翻译器独占前半段。

构造时传入的 `previous_candidates`（[translation.h:99](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/translation.h#L99)）是上游已产出的候选，供某些自定义 `Compare` 实现做上下文相关排序。

**`DistinctTranslation`：去重** —— [translation.cc:194-207](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/translation.cc#L194-L207)

```cpp
bool DistinctTranslation::Next() {
  candidate_set_.insert(Peek()->text());         // 记住刚输出的文字
  do {
    CacheTranslation::Next();
  } while (!exhausted() && AlreadyHas(Peek()->text()));  // 跳过重复
  return true;
}
```

它维护一个 `set<string> candidate_set_`，每次 `Next` 后若新候选的 `text` 已经输出过就继续跳过，从而保证流中**文字唯一**。注意它继承自 `CacheTranslation` 而非直接 `Translation`——因为「跳过重复」要多次调用 `Peek()` 判断，必须先缓存当前候选，否则在 `AlreadyHas` 与真正的 `Next` 之间候选会被底层流推进丢失。`uniquifier` filter 与 `Menu` 配合时常用到这类去重。

### 4.2.4 代码实践（本讲核心任务）

**实践目标**：对照源码，说明 `UnionTranslation`、`MergedTranslation`、`DistinctTranslation` 三者如何把多个翻译器的输出合并/去重。

**操作步骤**：

1. 打开 [src/rime/translation.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/translation.h)，定位三类声明：
   - `UnionTranslation`（[L70-81](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/translation.h#L70-L81)）：内部 `list<of<Translation>> translations_`，提供 `operator+=`。
   - `MergedTranslation`（[L85-102](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/translation.h#L85-L102)）：内部 `vector<of<Translation>>` 加 `elected_` 下标，构造需传入 `previous_candidates`。
   - `DistinctTranslation`（[L121-130](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/translation.h#L121-L130)）：继承 `CacheTranslation`，内部 `set<string> candidate_set_`。

2. 打开 [src/rime/menu.cc:15-20](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/menu.cc#L15-L20)，看 `Menu` 默认就用 `MergedTranslation` 归并：

   ```cpp
   Menu::Menu() : merged_(new MergedTranslation(candidates_)), result_(merged_) {}
   void Menu::AddTranslation(an<Translation> translation) {
     *merged_ += translation;        // 每来一个翻译器流就归并进来
   }
   ```

   说明：`Menu` 选 `MergedTranslation` 而非 `UnionTranslation`，是因为它要让多个翻译器的候选**按质量交叉排序**，而不是让某个翻译器独占前半段。

3. 自己画一张表，对比三者（见下方「预期结果」）。

**需要观察的现象**：在 `translation.cc` 中给 `MergedTranslation::Elect` 临时加一行 `LOG(INFO) << "elected #" << elected_;`（需开启 `ENABLE_LOGGING`），然后在 `rime_api_console` 输入 `ni`，观察日志里当选流下标如何在多个翻译器之间跳动。

**预期结果**（三个子类的对比表）：

| 子类 | 合并方式 | 是否排序 | 是否去重 | 典型用途 |
|---|---|---|---|---|
| `UnionTranslation` | 首尾串接（`[A]+[B]`） | 否（保入队序） | 否 | 明确「先 A 后 B」的优先级拼接 |
| `MergedTranslation` | k 路归并 | 是（按 `Candidate::compare`） | 否 | `Menu` 混合多翻译器候选 |
| `DistinctTranslation` | 包装单流 | 否（保持底层序） | 是（按 `text`） | 在归并后去重，或 filter 链中保证唯一 |

> 关于命名：任务里提到的 `DistinctTransaction` 在源码中实际叫 `DistinctTranslation`（见 [translation.h:121](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/translation.h#L121)），疑为笔误。

### 4.2.5 小练习与答案

**练习 1**：`UnionTranslation` 与 `MergedTranslation` 都能容纳多个流，它们的输出有何本质区别？

**答案**：`UnionTranslation` 是**串接**——把流 A 的全部候选输出完才轮到流 B，严格保序不交叉；`MergedTranslation` 是**归并**——每一步都从所有子流里选出当前最优（由 `Candidate::compare` 决定），各流候选交叉出现。前者适合固定优先级，后者适合按质量混合排序。

**练习 2**：`DistinctTranslation` 为什么继承自 `CacheTranslation` 而不是直接继承 `Translation`？

**答案**：去重逻辑需要在「记下当前 text」之后、真正推进之前多次 `Peek()` 来判断后续候选是否重复。若直接继承 `Translation`，每次 `Peek` 都会穿透到底层流，而 `Next` 才推进——多次 `Peek` 本身安全，但 `DistinctTranslation::Next` 内部要在「跳过重复」的循环里反复判断当前候选，缓存当前候选可避免与底层流状态耦合、也让 `Peek` 幂等。`CacheTranslation` 正好提供这份缓存。

**练习 3**：`MergedTranslation::Elect` 是如何用 `Compare` 实现「选优」的？

**答案**：它线性扫描所有子流，对每个流调用 `Compare(后继流, previous_candidates)`；若结果 `≤ 0` 表示「当前流不劣于后继」，就认为它是赢家并 `break`，记其下标为 `elected_`；若某流已耗尽则先删除再从头扫。最终 `Peek` 返回 `translations_[elected_]->Peek()`，等价于按 `compare` 的 k 路归并。

---

## 4.3 Composition：组织 Segment 与候选

### 4.3.1 概念说明

`Composition` 是这一层的「顶层视图」。它在 u3-l2 的 `Segmentation` 之上，**回答两个面向用户的问题**：

1. **当前整段输入最终要上屏什么文字？**（`GetCommitText`）
2. **预编辑区（preedit）里该怎么显示？**（`GetPreedit`）

注意 `Composition` 的继承关系（[composition.h:21](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/composition.h#L21)）：

```cpp
class Composition : public Segmentation { ... };
```

它**没有新增任何数据字段**，只是给 `Segmentation`（也就是「一串 `Segment`」）加了一组「把各段选中候选拼成文本」的方法。换句话说，`Composition` = `Segmentation`（段结构）+ 「每段的选中候选」。候选本身存在每个 `Segment::menu` 里（u3-l2 讲过 `Segment` 有个 `an<Menu> menu` 字段）。

为什么用继承而不是组合？因为 `Composition` 在语义上**就是一个带候选的 Segmentation**，引擎里 `Context::composition_` 的类型就是 `Composition`，切分逻辑（`Reset`/`Forward`/`AddSegment`）和候选拼接逻辑都作用在同一个对象上，继承让这两组方法自然合并到同一接口。

### 4.3.2 核心流程

把 4.1、4.2 串起来，看一次「按键 → 候选」的完整旅程：

```
Engine::Compose()
        │
        ├─ CalculateSegmentation()  ── segmentor 切出 Segment 序列（u3-l2）
        │
        └─ TranslateSegments()      ── 对每个 Segment：
                │
                ├─ New<Menu>()
                ├─ for 每个 translator:
                │     translation = translator->Query(input, segment)   返回 Translation
                │     menu->AddTranslation(translation)                 归并进 MergedTranslation
                ├─ for 每个 filter:
                │     menu->AddFilter(filter)                           用 filter 包装 result_
                └─ segment.menu = menu;  segment.status = kGuess        候选挂到段上
        │
        ▼
用户看到候选窗（Menu::CreatePage 按分页从 MergedTranslation 拉取 Candidate）
        │  用户选中第 i 个
        ▼
segment.selected_index = i;  segment.status = kSelected
        │
        ▼
Composition::GetCommitText()  ── 遍历所有段，取每段 GetSelectedCandidate().text() 拼接
```

关键的两段实现：

- **挂候选**：`ConcreteEngine::TranslateSegments`（[engine.cc:203-233](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L203-L233)）；
- **取提交文本**：`Composition::GetCommitText`（[composition.cc:102-120](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/composition.cc#L102-L120)）。

### 4.3.3 源码精读

先看 [`Composition` 的接口](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/composition.h#L14-L35)：

```cpp
// composition.h:14-35
struct Preedit {
  string text;
  size_t caret_pos = 0;
  size_t sel_start = 0;       // 高亮区起
  size_t sel_end = 0;         // 高亮区止
};

class Composition : public Segmentation {
 public:
  bool HasFinishedComposition() const;
  Preedit GetPreedit(const string& full_input, size_t caret_pos, const string& caret) const;
  string GetPrompt() const;
  string GetCommitText() const;        // 提交文本
  string GetScriptText(bool keep_selection = true) const;  // 带拼音的脚本文本
  string GetDebugText() const;         // 调试用
  string GetTextBefore(size_t pos) const;
};
```

`Preedit` 是个纯数据结构，描述「预编辑区显示什么」：文字 + 光标位置 + 高亮选中区间 `[sel_start, sel_end)`。

重点看 `GetCommitText`，它把各段选中候选拼成最终上屏文字（[composition.cc:102-120](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/composition.cc#L102-L120)）：

```cpp
string Composition::GetCommitText() const {
  string result;
  size_t end = 0;
  for (const Segment& seg : *this) {                // 遍历每段
    if (auto cand = seg.GetSelectedCandidate()) {   // 段有选中候选
      end = cand->end();
      result += cand->text();                       // 取候选文字
    } else {                                        // 无选中候选：回退到原始输入
      end = seg.end;
      if (!seg.HasTag("phony")) {                   // phony 段不上屏
        result += input_.substr(seg.start, seg.end - seg.start);
      }
    }
  }
  if (input_.length() > end) {                      // 尾部未被任何段覆盖的输入
    result += input_.substr(end);
  }
  return result;
}
```

逐段决策：若该段已被用户选中某候选（`GetSelectedCandidate()` 非空），就用候选的 `text()`；否则用原始输入串的对应片段——**除非**该段带 `phony` tag（`phony` 意为「假的」，这种段不产生上屏文字，例如某些纯提示段）。最后若输入串还有未被覆盖的尾巴，原样补上。

`Segment::GetSelectedCandidate` 的实现在 [segmentation.cc:45-53](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/segmentation.cc#L45-L53)，就是 `menu->GetCandidateAt(selected_index)`——这把本讲的三个类彻底串起来：`Composition` 通过 `Segment::menu` 找到 `Menu`，`Menu` 内部是 `MergedTranslation`，`MergedTranslation` 归并了各 `Translation`，每个 `Translation` 产出 `Candidate`。

再看候选是怎么挂到段上的，[`TranslateSegments`](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L203-L233)：

```cpp
// engine.cc:203-233（节选）
for (Segment& segment : *segments) {
  if (segment.status >= Segment::kGuess) continue;   // 已有候选则跳过
  string input = segments->input().substr(segment.start, len);
  auto menu = New<Menu>();
  for (auto& translator : translators_) {
    auto translation = translator->Query(input, segment);  // 4.2 的 Translation
    if (!translation || translation->exhausted()) continue;
    menu->AddTranslation(translation);                // 归并进 MergedTranslation
  }
  for (auto& filter : filters_) {
    if (filter->AppliesToSegment(&segment))
      menu->AddFilter(filter.get());                  // filter 包装 result_
  }
  segment.status = Segment::kGuess;
  segment.menu = menu;                                // 候选挂到段上
  segment.selected_index = 0;
}
```

这段代码是整条数据流的「组装车间」：对每个待翻译段，新建 `Menu`，让所有翻译器各显神通产出 `Translation` 并 `AddTranslation` 归并，再用适用的 `filter` 包装，最后把 `Menu` 挂到 `segment.menu`。注意 `if (!translation || translation->exhausted()) continue;`——翻译器返回空或「徒劳翻译」（exhausted）的会被跳过，这正是 [translation.cc:218-220](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/translation.cc#L218-L220) 那条 `DLOG` "made a futile translation" 的来源。

最后看 [`Menu::Prepare`](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/menu.cc#L26-L35)，它体现「按需拉取」：

```cpp
// menu.cc:26-35
size_t Menu::Prepare(size_t requested) {
  while (candidates_.size() < requested && !result_->exhausted()) {
    if (auto cand = result_->Peek()) candidates_.push_back(cand);  // 取一个
    result_->Next();                                               // 前进一步
  }
  return candidates_.size();
}
```

只有当已缓存的候选数 `candidates_.size()` 不足请求量 `requested` 时，才从归并流 `result_` 里 `Peek + Next` 继续拉取。这就是「用户翻到第几页，才查到第几页」的实现根基。

### 4.3.4 代码实践

**实践目标**：用 `rime_api_console` 亲眼看到 `Composition` 的内部结构，并对照 `GetDebugText` 理解段与候选的关系。

**操作步骤**：

1. 按 u1-l5 编译并运行 `tools/rime_api_console`，加载默认 luna_pinyin 方案。
2. 输入一段拼音（如 `nihao`），**先不要选候选**，观察 `context` 输出里的 `preedit` 与候选菜单。
3. 在源码 [engine.cc:168](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L168) 处可见 `DLOG(INFO) << "composition: [" << comp.GetDebugText() << "]";`。若用 `Debug` 构建并开启日志，能看到形如 `{abc}nihao=>你好` 的调试串——其中 `{abc}` 是 tag，`nihao` 是输入片段，`=>你好` 是选中候选的文字。
4. 选中一个候选（数字键），观察 `commit` 输出，对照 `GetCommitText` 的遍历逻辑验证它拼出的正是所选候选的 `text()`。

**需要观察的现象**：

- 未选候选时，`preedit` 显示的是拼音或第一候选，`commit` 为空；
- 选中后，对应 `Segment` 的 `status` 变为 `kSelected`，`GetCommitText` 产出该候选文字。

**预期结果**：能用手动追踪解释「输入 `nihao` → 看到候选『你好』 → 按键选中 → 上屏『你好』」这条链路在 `Composition`/`Menu`/`Translation`/`Candidate` 四层里分别对应哪一步。

> 若本地未配置日志构建，可只阅读源码并标注「待本地验证」日志现象；`GetCommitText`/`GetDebugText` 的逻辑可纯从源码推导。

### 4.3.5 小练习与答案

**练习 1**：`Composition` 继承 `Segmentation` 却不新增任何数据字段，这种设计的用意是什么？

**答案**：`Composition` 在语义上「就是一个带候选信息的 Segmentation」——段的切分结构（`Segmentation`）和「把各段选中候选拼成文本」的查询逻辑作用在同一个对象上。继承让切分方法（`Reset`/`Forward`/`AddSegment`）与候选拼接方法（`GetCommitText`/`GetPreedit`）自然合到一个接口，省去持有/转发 `Segmentation` 成员的样板代码。候选数据本身存在 `Segment::menu` 里，不需要 `Composition` 再加字段。

**练习 2**：`GetCommitText` 遇到带 `phony` tag 的段为什么会跳过、不上屏？

**答案**：`phony`（「假的」）标记表示该段是纯提示性质、不应产生真实上屏文字（例如某些编辑器临时插入的占位片段）。`GetCommitText` 对无选中候选的段默认回退到原始输入，但对 `phony` 段连原始输入也跳过，避免把不该上屏的内容混进提交文本。

**练习 3**：为什么说 `Menu::Prepare` 体现了「按需拉取」？

**答案**：`Prepare(requested)` 只在已缓存候选数不足 `requested` 时，才从底层归并流 `result_` 用 `Peek + Next` 继续拉取，拉够就停。这意味着用户翻第几页，引擎才查到第几页，未触及的候选不会被求值——这是 `Translation` 迭代器模式带来的惰性求值收益的直接体现。

---

## 5. 综合实践

把本讲三个模块串成一个追踪任务：**画出一次按键从「翻译器输出」到「上屏文字」的完整对象关系图**。

任务步骤：

1. 选定一个具体输入，例如在 luna_pinyin 方案下输入 `yi`。
2. 在源码中标注下列每一处对应的文件与行号，并说明它属于哪一层（`Candidate` / `Translation` / `Menu` / `Composition`）：
   - 翻译器造出候选：`New<SimpleCandidate>(...)`（参考 [trivial_translator.cc:44](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/sample/src/trivial_translator.cc#L44)，真实拼音方案在 `script_translator` 里类似）；
   - 候选被包成流：`New<UniqueTranslation>(candidate)`（[trivial_translator.cc:46](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/sample/src/trivial_translator.cc#L46)）；
   - 流被归并进菜单：`menu->AddTranslation(translation)`（[engine.cc:222](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L222)）；
   - 菜单挂到段：`segment.menu = menu`（[engine.cc:230](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L230)）；
   - 按需拉取候选：`Menu::Prepare`（[menu.cc:26](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/menu.cc#L26)）；
   - 选中后取文字：`Composition::GetCommitText`（[composition.cc:102](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/composition.cc#L102)）。
3. 用一张图把上述节点连起来，标注每条边上传递的对象类型（`an<Candidate>` / `an<Translation>` / `an<Menu>`）。
4. 进阶：在图上标出 `MergedTranslation` 与 `DistinctTranslation`/`UniquifiedCandidate` 各自介入的位置（归并、去重、合并同义候选）。

完成此任务后，你应该能不查源码就回答：「用户选中第 2 个候选时，引擎要走过哪些对象才能把它的文字提交出去？」

## 6. 本讲小结

- `Candidate` 是「一条候选项」的抽象基类，核心是纯虚的 `text()` 与可选的 `comment()`/`preedit()`；三个常用子类分别用不同策略提供文字——`SimpleCandidate`（自存）、`ShadowCandidate`（包装+覆写，用于繁简转换）、`UniquifiedCandidate`（合并同义候选）。
- `Candidate::compare` 给出确定性排序规则：段首优先 → 更长优先 → 质量优先；这是后续归并的基石。
- `Translation` 是「候选流」的惰性迭代器，接口为 `Peek()`/`Next()`/`exhausted()`；只有被 `Peek` 的候选才被求值，这让大词典下分页显示几乎零开销。
- `Translation` 一族组合子分三类：基本容器（`Unique`/`Fifo`）、多流合并（`Union` 串接保序、`Merged` 归并交叉排序）、装饰器（`Cache` 缓存、`Distinct` 去重、`Prefetch` 预填）。
- `Menu` 默认用 `MergedTranslation` 把多个翻译器的流归并，再串上 `Filter` 链，按 `Menu::Prepare` 按需拉取候选。
- `Composition` 继承 `Segmentation` 但不增数据字段，只提供「把各段选中候选拼成提交/预编辑文本」的方法（`GetCommitText`/`GetPreedit`/`GetScriptText`/`GetDebugText`）；候选通过 `Segment::menu` 间接持有。
- 整条数据流：`Engine::TranslateSegments` 为每段建 `Menu` → 各 translator 的 `Translation` 经 `AddTranslation` 归并 → filter 包装 → 挂到 `segment.menu` → 用户选中改 `selected_index` → `GetCommitText` 遍历各段拼出上屏文字。

## 7. 下一步学习建议

本讲把「候选如何产生与组织」讲到了 `Menu`/`Translation`/`Candidate` 这一层，但还有两个相邻问题悬而未决：

1. **`Menu` 如何分页产出候选、高亮索引如何计算？** —— 这正是下一篇 **u3-l4《Menu 与分页》** 的主题，它会展开 `Menu::Prepare`/`CreatePage` 的协作与 `page_size` 配置如何影响候选流。
2. **`Filter` 链具体做了什么？** 本讲只提到 `menu->AddFilter(filter)` 把 filter 串在 `result_` 上，但没讲 filter 如何包装 `Translation`。这会在 **u6-l5《Filter 组件族》** 里详细展开（如 `simplifier` 用 `ShadowCandidate`、`uniquifier` 用 `UniquifiedCandidate`）。

如果想立刻动手，建议跳到 **u9-l6《插件开发实战》**，参照 `trivial_translator` 写一个返回固定候选的自定义翻译器——它会逼你完整用到本讲的 `SimpleCandidate` + `UniqueTranslation` + `Query` 三件套，是把本讲知识固化为肌肉记忆的最快路径。
