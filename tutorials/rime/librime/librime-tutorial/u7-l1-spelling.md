# Spelling 与拼写属性

## 1. 本讲目标

本讲是「拼写代数与音节切分」单元（u7）的第一篇。上一篇 u6-l4 讲完了 Translator 如何把输入段翻译成候选词，但有一个关键细节我们一直把它当黑盒：**拼音输入 `ni` 为什么能匹配到 `你/尼/泥`，而 `n` 这种只打了首字母的输入又凭什么被接受、并且排在完整拼音后面？** 答案藏在每个候选背后的「拼写（Spelling）」上。

读完本讲，你应当能够：

1. 说出 `SpellingType` 六种取值（`kNormalSpelling`/`kFuzzySpelling`/`kAbbreviation`/`kCompletion`/`kAmbiguousSpelling`/`kInvalidSpelling`）各自的含义，并理解它们为何按数值大小排序。
2. 解释 `SpellingProperties` 的 `type`/`end_pos`/`credibility`/`tips`/`is_correction` 五个字段分别记录什么。
3. 区分 `Compose`（叠加）与 `Update`（合并）两条属性融合路径的语义差异，并能判断一段给定输入会走哪一条。
4. 说清楚一次 `fuzz`（模糊音）产生的拼写与一次 `abbrev`（简拼/缩写）产生的拼写，在 `type` 与 `credibility` 上到底有什么相同点和不同点。

## 2. 前置知识

本讲只依赖一个核心数据结构（`Spelling`），不涉及音节切分算法本身。但为了理解「拼写属性从哪来」，需要先具备以下直觉（均来自前置讲义）：

- **拼写代数（Spelling Algebra）**（u1-l1 引入）：方案 YAML 里 `speller/algebra` 段是一串 `xform`/`derive`/`erase`/`fuzz`/`abbrev` 等规则，它们把原始音节（如 `nihao`）变换出一批「派生拼写」（如简拼 `nh`、模糊音 `lihao`）。这些派生拼写会被一起建进 Prism 索引里，让用户用各种近似输入都能打出来。具体规则语法留待 u7-l2。
- **拉模型候选**（u3-l3、u3-l4）：候选是按需生成的，每个候选最终会绑定一条「它是由哪个拼写推导来的」信息，本讲的 `Spelling` 就是这条信息的载体。
- **`an<T>`/`of<T>` 智能指针别名**（u1-l3、u4-l1）：`common.h` 里 `an<T>` ≈ `shared_ptr<T>`，`string` ≈ `std::string`，看到这些不必困惑。

一个一句话总览：**`Spelling` 是「一条拼写串 + 一包描述它有多可信的属性」**。整条音节切分 → 词典查询 → 候选排序链路里，属性包决定了同一段输入下哪条拼写胜出、胜出后排第几。

## 3. 本讲源码地图

本讲只聚焦两个文件，它们都非常短：

| 文件 | 行数级别 | 作用 |
|------|---------|------|
| [src/rime/algo/spelling.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/spelling.h) | 约 50 行 | 定义 `SpellingType` 枚举、`SpellingProperties` 与 `Spelling` 三个数据结构 |
| [src/rime/algo/spelling.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/spelling.cc) | 约 40 行 | 实现 `Compose` 与 `Update` 两个属性融合方法 |

为了说明「这些属性是谁产生的、怎么被消费的」，本讲会**引用但不深入**以下周边文件（属于后续讲义 u7-l2/u7-l3）：

- `src/rime/algo/calculus.cc`：拼写代数运算 `fuzz`/`abbrev` 在此给 `type` 和 `credibility` 赋值。
- `src/rime/algo/algebra.cc`：`Script::Merge` 在此调用 `Compose`/`Update`。
- `src/rime/algo/syllabifier.cc`：音节切分器消费这些属性做候选筛选与排序。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 `Spelling` 与 `SpellingProperties` 数据模型**：先认清「一条拼写长什么样」。
- **4.2 `SpellingType` 拼写类型与权重阶梯**：理解六个枚举值的含义与排序约定。
- **4.3 `Compose` 与 `Update` 两条合并路径**：本讲的重头戏，回答「多条派生拼写属性如何汇成一条」。

---

### 4.1 `Spelling` 与 `SpellingProperties` 数据模型

#### 4.1.1 概念说明

在拼音输入法里，用户敲下的串（比如 `n`）往往能对应多种「解释」：

- 它可能是完整拼音 `n`（极少单字）；
- 它可能是某个完整拼音的**缩写**，比如 `ni`/`na`/`nan` 的首字母；
- 它可能是某个拼音的**补全**开头，比如用户想打 `ni` 但只敲到 `n`。

对同一个用户输入，引擎会同时算出好几条「拼写候选」，每条都需要记录两样东西：

1. **拼写串本身**（`str`）：例如 `"ni"`、`"nh"`。
2. **这条拼写的「出身」信息**（`properties`）：它是模糊音吗？是缩写吗？可信度多高？要不要给用户一个提示？

`Spelling` 就是把这两者打包的结构体；`SpellingProperties` 是塞在它里面的那个「属性包」。两者是**组合关系**（has-a），不是继承。

#### 4.1.2 核心流程

一个 `Spelling` 对象的生命周期大致是：

```
原始音节 "ni"
   │  （拼写代数 derive/abbrev/fuzz 等规则派生）
   ▼
派生出多条 Spelling: {"ni", props=Normal}、{"n", props=Abbrev}、{"li", props=Fuzzy} ...
   │  （多条派生拼写属性汇合，走 Compose / Update）
   ▼
最终每条拼写携带一份融合后的 properties
   │  （Prism 建索引 / Syllabifier 切分 / Translator 取候选）
   ▼
候选词排序时按 properties.credibility 与 type 加权
```

注意 `Spelling` 的比较运算符**只比较拼写串 `str`**，不看属性——这决定了「去重」的粒度是按字符串，两条 `str` 相同但属性不同的拼写会被视作同一个拼写的不同来源而触发合并（这正是 4.3 要讲的内容）。

#### 4.1.3 源码精读

`SpellingProperties` 与 `Spelling` 都定义在 [src/rime/algo/spelling.h:24-46](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/spelling.h#L24-L46)：

```cpp
struct SpellingProperties {
  SpellingType type = kNormalSpelling;  // 拼写类型，默认「正常」
  size_t end_pos = 0;                   // 这条拼写消费到输入串的第几个字节
  double credibility = 0.0;             // 可信度（对数空间，越大越可信，0 最佳）
  string tips;                          // 给用户看的提示文本（如纠错时显示原串）
  bool is_correction = false;           // 是否为纠错（用户打错了，算法帮改对了）

  void Compose(const SpellingProperties& delta);  // 叠加：把一条运算规则产生的属性并进来
  void Update(const SpellingProperties& other);   // 合并：把另一条同串候选拼写并进来
};

struct Spelling {
  string str;                // 拼写串，如 "ni"
  SpellingProperties properties;

  Spelling() = default;
  Spelling(const string& _str) : str(_str) {}

  bool operator==(const Spelling& other) { return str == other.str; }  // 只比 str
  bool operator<(const Spelling& other) { return str < other.str; }    // 只比 str
};
```

逐字段说明：

- `type`：枚举，本讲 4.2 详解。默认 `kNormalSpelling`，意味着「这是一条原原本本的合法拼写」。
- `end_pos`：记录这条拼写在输入串里吃掉了多少个字节。音节切分时（u7-l3），`SyllableGraph` 的边会用到它来定位 `[start, end)` 区间。
- `credibility`：**对数可信度**，`0.0` 表示完全可信，负得越多越不可信。它是排序的核心权重，本讲 4.2 会结合惩罚常数讲清楚数学含义。
- `tips`：可选的提示文本。典型场景是**纠错**：用户打了 `nihao` 但词典里只有 `lihao`，纠错器会生成一条指向 `lihao` 的拼写，并在 `tips` 里存上原始串 `nihao`，前端据此给用户一个「你是不是想打…」的提示。可以参考 [src/rime/dict/corrector.cc:93-95](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/corrector.cc#L93-L95) 里 `spelling.properties.tips = origin;` 的写法。
- `is_correction`：布尔标记，专门标记「这是纠错产生的」。它和 `tips` 配合使用，但语义更轻——`tips` 可空，`is_correction` 只是个开关。

`operator==` / `operator<` 只看 `str`，这是关键设计：它让 `std::find` / `std::map` 按**字符串**去重和定位，属性差异不会制造出多个「拼写条目」，而是在同一条目内做属性合并。

#### 4.1.4 代码实践

**实践目标**：用一段最小的 C++ 片段，亲手构造两个 `Spelling`，验证 `operator==` 只看 `str`。

**操作步骤**（源码阅读 + 心智推演型，无需编译运行）：

1. 在 [src/rime/algo/spelling.h:37-46](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/spelling.h#L37-L46) 确认 `operator==` 的实现体只有 `str == other.str`。
2. 推演下面这段「示例代码」（非项目原有代码，仅为说明）的输出：

```cpp
// 示例代码
rime::Spelling a("ni");
a.properties.type = rime::kNormalSpelling;
a.properties.credibility = 0.0;

rime::Spelling b("ni");
b.properties.type = rime::kAbbreviation;
b.properties.credibility = -0.693;

bool same = (a == b);  // 期望: true
```

**需要观察的现象**：尽管 `a` 和 `b` 的 `type`、`credibility` 完全不同，`a == b` 仍为 `true`，因为两者 `str` 都是 `"ni"`。

**预期结果**：`same == true`。这正是后续 `Script::Merge` 用 `std::find` 能定位到同串拼写、进而调用 `Update` 合并属性的前提。

> 待本地验证：若你想真正运行，需在 librime 构建树里写一个链接 `rime` 库的小测试，本讲不展开编译步骤。

#### 4.1.5 小练习与答案

**练习 1**：`SpellingProperties` 里 `credibility` 的默认值是 `0.0`，为什么「最大值」反而是可信度最高的？

**答案**：因为 `credibility` 工作在**对数空间**，且各种派生（模糊、缩写、补全、纠错）都是给它**减**一个惩罚（负数）。原始合法拼写的可信度最高，对应 `0.0`（即 \(\log 1\)，概率为 1）；任何派生都让它变小（更负）。所以越接近 0 越可信。

**练习 2**：如果把 `Spelling::operator==` 改成同时比较 `str` 和 `properties.type`，会对候选去重产生什么影响？

**答案**：那么 `str` 相同但 `type` 不同的拼写会被当成两个独立条目，无法合并属性，`Script::Merge` 里的 `std::find` 将找不到已存在的同串拼写，导致同一条拼写以多个条目重复进入索引，既浪费空间也可能让排序失真。当前「只比 `str`」的设计正是为了让属性合并（`Update`）有发生的契机。

---

### 4.2 `SpellingType` 拼写类型与权重阶梯

#### 4.2.1 概念说明

`SpellingType` 是一个六值枚举，给每条拼写贴上「出身标签」。它有两个用途：

1. **分类**：告诉下游（音节切分器、翻译器）这条拼写属于哪一类，据此决定是否采纳。
2. **排序**：枚举值本身按「从最可信到最不可信」**单调递增**排列，所以代码里大量出现 `type > X`、`(std::min)(it->second.type, props.type)` 这类比较，直接用枚举的数值大小当排序键。

#### 4.2.2 核心流程

六个枚举值的数值与含义（按可信度从高到低）：

| 枚举值 | 数值 | 含义 | 典型来源 |
|--------|------|------|---------|
| `kNormalSpelling` | 0 | 正常完整拼写，用户原样输入 | 原始音节、`xform` |
| `kFuzzySpelling` | 1 | 模糊音，如 zh/z 不分、n/l 不分 | `fuzz` 规则 |
| `kAbbreviation` | 2 | 简拼/缩写，如用 `nh` 代表 `nihao` | `abbrev` 规则 |
| `kCompletion` | 3 | 补全，用户没打完，算法猜的 | `Syllabifier` 前缀扩展 |
| `kAmbiguousSpelling` | 4 | 切分歧义，一段编码可被多种切分解释 | `Syllabifier` 重叠检测 |
| `kInvalidSpelling` | 5 | 无效 | 兜底/标记用 |

由于数值随不可信度递增，源码里比较「谁更优」就是比谁**更小**。例如音节切分器里有 `(std::min)(it->second.type, props.type)`——取更小（更可信）的那个类型。

#### 4.2.3 源码精读

枚举定义在 [src/rime/algo/spelling.h:15-22](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/spelling.h#L15-L22)：

```cpp
enum SpellingType {
  kNormalSpelling,    // 0
  kFuzzySpelling,     // 1
  kAbbreviation,      // 2
  kCompletion,        // 3
  kAmbiguousSpelling, // 4
  kInvalidSpelling    // 5
};
```

**`type` 如何被生产端赋值**——以拼写代数运算为例，`fuzz` 和 `abbrev` 在应用成功后显式设置 `type` 并扣减 `credibility`，见 [src/rime/algo/calculus.cc:202-232](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/calculus.cc#L202-L232)：

```cpp
bool Fuzzing::Apply(Spelling* spelling) {
  bool result = Transformation::Apply(spelling);  // 先做正则替换
  if (result) {
    spelling->properties.type = kFuzzySpelling;                 // 标记为模糊音
    spelling->properties.credibility += kFuzzySpellingPenalty;  // 扣可信度
  }
  return result;
}

bool Abbreviation::Apply(Spelling* spelling) {
  bool result = Transformation::Apply(spelling);
  if (result) {
    spelling->properties.type = kAbbreviation;                 // 标记为缩写
    spelling->properties.credibility += kAbbreviationPenalty;  // 扣可信度
  }
  return result;
}
```

这里用到的惩罚常数定义在 [src/rime/algo/calculus.cc:14-16](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/calculus.cc#L14-L16)：

```cpp
const double kAbbreviationPenalty = -0.6931471805599453;   // log(0.5)
const double kFuzzySpellingPenalty = -0.6931471805599453;  // log(0.5)
const double kCorrectionPenalty = -4.605170185988091;      // log(0.01)
```

**数学含义**：`credibility` 是自然对数空间下的对数概率。一次模糊或一次缩写相当于「这条拼写成立的概率乘以 0.5」，即 \(\log 0.5 \approx -0.693\)；一次纠错相当于「概率乘以 0.01」，即 \(\log 0.01 \approx -4.605\)。补全的惩罚定义在音节切分器里，见 [src/rime/algo/syllabifier.cc:27-28](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/syllabifier.cc#L27-L28)：

```cpp
const double kCompletionPenalty = -2.995732273553991;      // log(0.05)
const double kCorrectionCredibility = -4.605170185988091;  // log(0.01)
```

之所以用对数，是因为多条规则叠加时概率要**相乘**，而 \(\log(p_1 \cdot p_2) = \log p_1 + \log p_2\)——所以 `Compose` 里 `credibility` 用加法就能正确合成概率，下一节会看到。

补全类型是在音节切分阶段动态赋予的，见 [src/rime/algo/syllabifier.cc:224-227](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/syllabifier.cc#L224-L227)：

```cpp
SpellingProperties props = accessor.properties();
if (props.type < kAbbreviation) {        // 只有「较可信」的拼写才升级成补全
  props.type = kCompletion;
  props.credibility += kCompletionPenalty;
  props.end_pos = end_pos;
```

注意这个 `if (props.type < kAbbreviation)` 的守卫：它确保**只有本来就比缩写更可信的拼写**（正常、模糊）才会被标记为「补全」。已经被标记为缩写或更差的，不再降级——这体现了类型阶梯的严格性。

#### 4.2.4 代码实践

**实践目标**：对照真实方案的 `speller/algebra` 规则，逐条判断它会产出哪种 `SpellingType`。

**操作步骤**：

1. 打开 [data/minimal/luna_pinyin.schema.yaml:73-86](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.schema.yaml#L73-L86)，找到 `speller/algebra` 列表。
2. 对每条规则，根据它的首个 token（`erase`/`abbrev`/`derive`/`fuzz`/`xform`）以及是否带 `correction` 后缀，判断它给派生拼写赋予的 `type` 与 `credibility` 增量。

**需要观察的现象 / 预期结果**（下表为分析结论）：

| 规则 | 产出 type | credibility 增量 |
|------|----------|-----------------|
| `erase/^xx$/` | （删除，不产出） | — |
| `abbrev/^([a-z]).+$/$1/` | `kAbbreviation` | `log(0.5)` |
| `abbrev/^([zcs]h).+$/$1/` | `kAbbreviation` | `log(0.5)` |
| `derive/^([nl])ve$/$1ue/correction` | `kNormalSpelling` + `is_correction=true` | `log(0.01)` |
| `xform/...` | `kNormalSpelling` | 0（替换原串，不扣分） |

关键观察：`fuzz` 在 `luna_pinyin` 默认 algebra 里**没有出现**（模糊音默认关闭），但它的语义和 `abbrev` 几乎对称——这正是下一节要回答的核心问题。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `kNormalSpelling` 的数值是 0，而不是 1？

**答案**：因为枚举按「可信度从高到低」递增数值，最可信的排在最前，C++ 枚举默认从 0 开始。这样「更可信」就等于「数值更小」，源码里用 `(std::min)` 取最优类型、用 `type > kXxx` 做「是否差于某阈值」的判断都能直接复用数值比较。

**练习 2**：`kCompletion`（补全，数值 3）比 `kAbbreviation`（缩写，数值 2）数值更大，意味着补全比缩写「更不可信」。结合生活直觉，这个排序合理吗？

**答案**：合理。缩写是用户**主动**敲的（比如故意只敲首字母 `nh`），用户知道自己在干什么；补全是用户**还没敲完**时算法替他猜的（敲了 `ni` 算法猜他可能要打 `nihao`），猜错的概率更高。所以补全的可信度低于缩写，数值更大、惩罚更重（`log(0.05)` vs `log(0.5)`）。

---

### 4.3 `Compose` 与 `Update` 两条合并路径

#### 4.3.1 概念说明

一条原始音节经过一串拼写代数规则后，会派生出多条拼写。这些派生拼写最终要汇进一个 `Script`（本质是 `map<string, vector<Spelling>>`，见 [src/rime/algo/algebra.h:20](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/algebra.h#L20)）。汇合时会出现两种场景，对应两个方法：

- **`Compose(delta)`——叠加**：同一条派生拼写，由「运算规则本身」带来的属性增量（`delta`）要叠到它**继承自父拼写的属性**上。例如 `abbrev` 规则把 `nihao` 变成 `nh`，`nh` 这条新拼写既要继承 `nihao` 原本可能有的属性，又要叠加「我是一次缩写」这个 `delta`。
- **`Update(other)`——合并**：两条**拼写串相同**但来源不同的拼写要合并成一条，取两者属性的最优组合。例如 `ni` 这个串可能同时由「原始音节」和「某模糊规则」产生，合并时要挑出更可信的那个 `type`、更大的 `credibility`。

一句话区分：**`Compose` 是「父 + 规则增量 → 子」的纵向叠加；`Update` 是「兄弟之间」的横向择优合并。**

#### 4.3.2 核心流程

两个方法的字段处理对照（`delta`/`other` 是输入，左侧是被修改的当前对象）：

```
Compose(delta)：                Update(other)：
  type      = max(type, delta.type)     | 相同 type: is_correction = AND
                                       | 否则采纳更小(更优)的 type，is_correction 跟随
  credibility += delta.credibility      | credibility = max(credibility, other.credibility)
  is_correction |= delta.is_correction  | (同 type 时同步)
  tips      = delta.tips（非空则覆盖）   | tips.clear()   ← 直接清空！
```

两条路径对 `tips` 的处理截然相反，这是理解它们语义差异的钥匙：

- `Compose` 认为 `delta` 带来的 `tips` 是这条拼写的正当属性，**保留并覆盖**。
- `Update` 认为合并后的拼写是「混合体」，原本针对某一来源的 `tips` 不再适用，**直接清空**（源码注释原话：「提示是針對其中一個拼寫來源的，可能對合併後的拼寫不適用」）。

#### 4.3.3 源码精读

`Compose` 实现在 [src/rime/algo/spelling.cc:5-20](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/spelling.cc#L5-L20)：

```cpp
void SpellingProperties::Compose(const SpellingProperties& delta) {
  // 類型取最模糊者
  if (delta.type > type) {
    type = delta.type;
  }
  // 權重累加
  credibility += delta.credibility;
  // 糾錯標記
  if (delta.is_correction) {
    is_correction = true;
  }
  if (!delta.tips.empty()) {
    tips = delta.tips;
  }
}
```

四个要点：

1. **`type` 取 `max`**：因为派生拼写不会比它的「出身」更可信。如果父拼写是正常（0）、规则 `delta` 是缩写（2），结果是缩写（2）。这保证了一条拼写只要经过任何「降级」规则，类型就跟着降级。
2. **`credibility` 累加**：对应概率相乘（对数相加），数学上自洽。
3. **`is_correction` 单向置真**：只要 `delta` 是纠错，结果就标记为纠错（`|=` 语义）。
4. **`tips` 覆盖**：`delta` 非空就覆盖，空则保留原值。

`Update` 实现在 [src/rime/algo/spelling.cc:22-39](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/spelling.cc#L22-L39)：

```cpp
void SpellingProperties::Update(const SpellingProperties& other) {
  // 類型相同, 以權重高者爲主, 同步糾錯標記
  if (type == other.type) {
    is_correction = is_correction && other.is_correction;
  }
  // 採納更優的類型
  else if (other.type < type) {
    type = other.type;
    is_correction = other.is_correction;
  }
  // 保留最大權重
  if (other.credibility > credibility) {
    credibility = other.credibility;
  }
  // 提示是針對其中一個拼寫來源的, 可能對合併後的拼寫不適用
  tips.clear();
}
```

四个要点：

1. **`type` 相同**：不改变 `type`，但把 `is_correction` 做逻辑与（两者都得是纠错，合并后才算纠错）。
2. **`type` 不同且 `other` 更优**：采纳 `other.type`，`is_correction` 完全跟随 `other`（因为类型变了，旧标记作废）。
3. **若 `other.type` 更差则不动**：保留当前更优的 `type`（这个分支隐含在 `if/else if` 都不命中时）。
4. **`credibility` 取 `max`**：与 `Compose` 的累加不同，合并时取更可信者。**`tips` 无条件清空**。

**调用现场**——两者都在 `Script::Merge` 里被用到，见 [src/rime/algo/algebra.cc:22-38](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/algebra.cc#L22-L38)：

```cpp
void Script::Merge(const string& s,
                   const SpellingProperties& sp,
                   const vector<Spelling>& v) {
  vector<Spelling>& m((*this)[s]);
  for (const Spelling& x : v) {
    Spelling y(x);
    y.properties.Compose(sp);                   // ① 纵向叠加规则增量
    auto e = std::find(m.begin(), m.end(), x);  //    按 str 查找已有同串拼写
    if (e == m.end()) {
      m.push_back(y);                           //    没有就新增
    } else {
      e->properties.Update(y.properties);       // ② 横向合并同串兄弟
    }
  }
}
```

这段代码完整展示了「先 `Compose` 再视情况 `Update`」的协作：每条来自父拼写 `v` 的派生拼写 `y`，先用规则增量 `sp` 做 `Compose`；然后到目标列表 `m` 里按 `str` 查找——若 `str` 已存在就 `Update` 合并，否则直接 `push_back`。注意 `std::find` 用的就是 4.1 讲的「只比 `str`」的 `operator==`，三者环环相扣。

#### 4.3.4 代码实践

**实践目标**：回答本讲开篇的问题——一次 `fuzz`（模糊音）与一次 `abbrev`（缩写）产生的 `Spelling`，在 `type` 与 `credibility` 上有何异同。

**操作步骤**：

1. 读 [src/rime/algo/calculus.cc:14-16](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/calculus.cc#L14-L16) 的两个惩罚常数，确认 `kAbbreviationPenalty` 与 `kFuzzySpellingPenalty` 的值。
2. 读 [src/rime/algo/calculus.cc:202-209](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/calculus.cc#L202-L209)（`Fuzzing::Apply`）与 [src/rime/algo/calculus.cc:226-232](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/calculus.cc#L226-L232)（`Abbreviation::Apply`），对比两者给 `type` 与 `credibility` 的赋值方式。

**需要观察的现象 / 预期结论**：

设父拼写 `credibility = 0`、`type = kNormalSpelling`，分别施加一条 `fuzz` 与一条 `abbrev`：

| 维度 | `fuzz` 产生的拼写 | `abbrev` 产生的拼写 |
|------|------------------|--------------------|
| `type` | `kFuzzySpelling` (1) | `kAbbreviation` (2) |
| `credibility` | \(0 + \log 0.5 \approx -0.693\) | \(0 + \log 0.5 \approx -0.693\) |
| `is_correction` | `false` | `false` |
| `tips` | 空 | 空 |

**关键结论**：

- **`credibility` 完全相同**：两者都扣 `log(0.5)`，因为惩罚常数定义成了同一个值。
- **`type` 不同**：`fuzz` 是 `kFuzzySpelling`(1)，`abbrev` 是 `kAbbreviation`(2)。也就是说，**仅凭 `credibility` 无法区分模糊音与缩写，差异完全体现在 `type` 上**。
- 这意味着下游若想对两者区别对待（例如 `strict_spelling` 模式下拒绝缩写却保留模糊音），只能依据 `type`，不能依据 `credibility`。可参考 [src/rime/algo/syllabifier.cc:110-112](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/syllabifier.cc#L110-L112) 里 `props.type != kNormalSpelling` 这类按 `type` 做筛选的写法。

**进一步推演**（待本地验证）：若把这两条拼写（`str` 相同）再 `Update` 合并，按 4.3.3 规则，`type` 会取更优者 `kFuzzySpelling`(1)，`credibility` 取 `max`（两者相等故不变）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Compose` 对 `credibility` 用加法，而 `Update` 用 `max`？

**答案**：`Compose` 是「父拼写属性 + 规则增量」的纵向叠加，对应概率相乘（对数相加）——父拼写本身有概率，规则又是一个条件概率，合起来要相乘，故用加法。`Update` 是「两个独立来源的同串拼写」择优保留，二者是竞争关系而非条件关系，应取更可信的那个，故用 `max`。

**练习 2**：若一条拼写先 `Compose` 了一个带 `tips = "nihao"` 的纠错增量，随后又因为 `str` 撞上另一条拼写触发了 `Update`，最终它的 `tips` 会是什么？

**答案**：会被清空。`Update` 末尾无条件 `tips.clear()`，源码注释说明合并后的拼写是混合体，原 `tips` 只针对单一来源、不再适用，故丢弃。这正是 `Compose`（保留覆盖）与 `Update`（清空）在 `tips` 处理上最鲜明的对比。

**练习 3**：`Compose` 中 `type` 取 `max`，会不会出现「父拼写是 `kCompletion`(3)、规则 `delta` 是 `kNormalSpelling`(0)，结果变成 0」的情况？

**答案**：不会。`Compose` 只在 `delta.type > type` 时才更新 `type`，即只升不降。父拼写是 3、`delta` 是 0 时 `0 > 3` 为假，`type` 保持 3。语义合理：一条已经「补全」级别的拼写，不会因为套了个正常规则就变回完全可信。

---

## 5. 综合实践

把本讲三个模块串起来，做一个完整的「属性追踪」推演任务。

**任务**：给定 `luna_pinyin` 方案的 `speller/algebra` 中这两条规则（见 [data/minimal/luna_pinyin.schema.yaml:75-76](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.schema.yaml#L75-L76)）：

```yaml
- abbrev/^([a-z]).+$/$1/
- abbrev/^([zcs]h).+$/$1/
```

原始音节是 `"zhang"`（`type=kNormalSpelling`, `credibility=0`）。请推演：

1. 经过第一条 `abbrev` 规则后，派生出拼写 `"z"`，它的 `type` 与 `credibility` 分别是什么？
2. 经过第二条 `abbrev` 规则后，派生出拼写 `"zh"`，它的 `type` 与 `credibility` 分别是什么？
3. 假设这两条规则在不同轮次里都对 `"zhang"` 产生过拼写 `"z"`（第二条 `^([zcs]h)` 匹配 `zh`，不匹配 `z`，故仅第一条产生 `z`——请据正则判断此假设是否成立），如果 `Script::Merge` 里 `str="z"` 撞重，会走 `Compose` 还是 `Update`？最终 `credibility` 如何？

**参考解答**：

1. `"z"`：`type = kAbbreviation`(2)，`credibility = 0 + log(0.5) ≈ -0.693`。
2. `"zh"`：`type = kAbbreviation`(2)，`credibility = 0 + log(0.5) ≈ -0.693`。
3. 第二条规则正则 `^([zcs]h).+$` 要求首字母是 z/c/s 且第二个字符是 h，匹配 `zhang` 得到捕获组 `zh`，**不会**产生 `z`。所以 `str="z"` 只来自第一条规则，不会触发合并。但作为思维训练：若假设它真撞重，`Merge` 里**先对每条做 `Compose`**（叠加 `abbrev` 的 `delta`），**再按 `str` 查找，撞重则 `Update`**。两次都是 `kAbbreviation` 同类型，`Update` 对同类型不改变 `type`，`credibility` 取 `max`（两边都是 -0.693，结果不变），`tips` 被清空。

这个练习把「规则产属性（4.2）→ `Compose` 叠加（4.3）→ `Update` 合并（4.3）→ 类型阶梯与对数权重（4.2/4.1）」全部串了起来。

## 6. 本讲小结

- `Spelling` = 拼写串 `str` + 属性包 `properties`（`SpellingProperties`），两者组合关系；`operator==`/`operator<` 只比 `str`，决定了去重按字符串进行。
- `SpellingProperties` 五字段：`type`（出身标签）、`end_pos`（消费到的字节位置）、`credibility`（对数可信度，0 最佳）、`tips`（提示文本）、`is_correction`（纠错标记）。
- `SpellingType` 六值按可信度从高到低递增（`kNormalSpelling`=0 … `kInvalidSpelling`=5），源码直接用数值比较当排序键，`(std::min)` 即「取更优类型」。
- `credibility` 工作在自然对数空间：模糊、缩写惩罚为 \(\log 0.5\)，补全为 \(\log 0.05\)，纠错为 \(\log 0.01\)；对数让概率相乘退化为加法。
- **`fuzz` 与 `abbrev` 的 `credibility` 完全相同**（都是 \(\log 0.5\)），区别只在 `type`（`kFuzzySpelling` vs `kAbbreviation`），下游只能靠 `type` 区分二者。
- `Compose`（纵向叠加，父+规则增量）：`type` 取 max、`credibility` 累加、`tips` 覆盖；`Update`（横向择优，兄弟合并）：`type` 取更优、`credibility` 取 max、`tips` 清空。两者在 `tips` 处理上截然相反，是区分语义的钥匙。

## 7. 下一步学习建议

本讲只讲了「拼写属性是什么、怎么合并」，还没有讲「这些属性是怎么被一串正则规则批量生产出来的」。建议按顺序继续：

- **u7-l2 Calculus 与拼写运算**：精读 `calculus.cc` 里 `Transformation`/`Erasion`/`Derivation`/`Fuzzing`/`Abbreviation`/`Transliteration` 六类 `Calculation` 的 `Parse` 与 `Apply`，以及 `Projection` 如何对整个 `Script` 批量施加规则。本讲引用的 [src/rime/algo/calculus.cc:202-232](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/calculus.cc#L202-L232) 会在那里完整展开。
- **u7-l3 Syllabifier 与音节图**：精读 `syllabifier.cc`，看 `BuildSyllableGraph` 如何基于 Prism 把输入串切成所有可能的音节组合，以及 `kCompletion`/`is_correction` 在图构建阶段如何被动态赋予并影响候选筛选。本讲多次引用的 [src/rime/algo/syllabifier.cc:224-227](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/syllabifier.cc#L224-L227) 是它的入口之一。

如果想在更上层看这些拼写属性如何影响最终候选排序，可回看 u6-l4（Translator 族）中 `script_translator` 沿 `SyllableGraph` 查词典的部分。
