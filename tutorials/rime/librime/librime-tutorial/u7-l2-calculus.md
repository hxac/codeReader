# Calculus 与拼写运算

## 1. 本讲目标

本讲拆解 librime「拼写代数（Spelling Algebra）」的执行内核：`Calculus` 与 `Calculation`。

学完本讲你应该能够：

- 说清一条形如 `xform/x/y/`、`derive/^nve$/nue/correction` 的「公式串」是如何被解析成一个 C++ 对象的。
- 列出六类运算（`xlit`/`xform`/`erase`/`derive`/`fuzz`/`abbrev`，外加隐藏的 `correction`）各自的语义、继承关系与对 `Spelling` 属性的副作用。
- 解释 `addition()`/`deletion()` 这对虚函数如何用同一套循环统一表达「替换 / 增补 / 删除」三种行为。
- 区分 `Projection::Apply` 的两种重载：对单个字符串的「显示期格式化」与对整张 `Script` 拼写表的「构建期派生」。
- 对照真实方案 `luna_pinyin.schema.yaml` 的 `speller/algebra`，逐条标注每条规则属于哪一类运算。

本讲是 u7-l1（`Spelling` 与拼写属性）的直接延续：u7-l1 讲清了「一条拼写长什么样」，本讲讲清「这些拼写是从哪里、被什么规则派生出来的」。

## 2. 前置知识

在进入源码前，先建立两个直觉。

**直觉一：拼写代数是「一个音节 → 多种可接受拼写」的展开器。**

拼音里同一个音节「略」在词典里记作 `lve`（用 `v` 表示 `ü`）。但用户可能敲 `lue`、甚至只敲首字母 `l`。拼写代数的作用，就是在「构建词典索引」时，对每一个规范音节施加一串规则，派生出它的模糊音、缩写、纠错变体，让这些变体都指向同一个音节。展开后的结果就是一张「拼写 → 音节」的索引（即 `Prism`，见 u8-l2）。

**直觉二：公式串是一种极简的 DSL。**

RIME 不让你写 C++ 来定义派生规则，而是让你在 YAML 里写一行字符串：

```
- derive/^([nl])ve$/$1ue/correction
```

这一行被 `Calculus::Parse` 解析后，会变成一个 `Correction` 类型的 `Calculation` 对象。本讲的核心就是搞懂「这行字符串 → 这个对象 → 它如何改写拼写」的完整链条。

还需要回忆 u7-l1 的两个结论：

- `Spelling` = 拼写串 `str` + 属性包 `SpellingProperties`（含 `type`、`credibility`、`tips`、`is_correction`）。
- `credibility` 工作在**自然对数空间**：惩罚写成 `log(p)`，相乘的概率退化为相加。`log(0.5) ≈ -0.693`，`log(0.01) ≈ -4.605`。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/rime/algo/calculus.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/calculus.h) | 声明 `Calculation` 抽象基类、`Calculus` 注册表，以及六类运算的类层次。 |
| [src/rime/algo/calculus.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/calculus.cc) | 实现 `Calculus::Parse` 的分隔符切分、各运算的 `Parse`/`Apply`，以及惩罚常数。 |
| [src/rime/algo/algebra.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/algebra.h) | 声明 `Script`（拼写表）与 `Projection`（运算序列），是 `Calculus` 的批量外壳。 |
| [src/rime/algo/algebra.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/algebra.cc) | 实现 `Script::AddSyllable/Merge/Dump`、`Projection::Load` 与两种 `Apply`。 |
| data/minimal/luna_pinyin.schema.yaml | 真实方案的 `speller/algebra` 规则集，本讲实践的对象。 |

调用方（说明这套机制用在哪）：构建期在 [dict_compiler.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc) 里用 `Projection::Apply(Script*)` 展开音节；显示期在 [translator_commons](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/translator_commons.h) 里用 `Projection::Apply(string*)` 格式化 preedit/comment。

## 4. 核心概念与源码讲解

### 4.1 Calculus 注册表与 Calculation 抽象基类

#### 4.1.1 概念说明

`Calculation` 是「一次拼写运算」的抽象基类。它的核心契约只有一个纯虚函数 `Apply(Spelling*)`：把一个拼写改写成另一个拼写（或清空它），返回是否发生了改变。

`Calculus` 是一个「名字 → 工厂函数」的注册表。它只做两件事：

- `Register(token, factory)`：把一个运算名（如 `"xform"`）绑定到一个工厂函数。
- `Parse(definition)`：把一条公式串交给对应工厂，造出一个 `Calculation*`。

这套设计与 u5-l1 讲过的 `Registry`/`Component` 体系形似——都是「按名取货」——但这里注册的不是带状态的组件实例，而是无状态的工厂函数指针，因为 `Calculation` 本身是轻量、可复用的算子。

#### 4.1.2 核心流程

```
Calculus 构造时：把 6 个内置运算名各绑定到一个 Parse 工厂
运行时：
  Parse("derive/^nve$/nue/correction")
    -> 找到分隔符 -> 切成 args -> 用 args[0]="derive" 查 factories_
    -> 调用 Derivation::Parse(args) -> 返回 Correction 对象
  对象->Apply(&spelling)  // 真正改写拼写
```

#### 4.1.3 源码精读

`Calculation` 基类定义了三个关键虚函数。注意 `Apply` 是纯虚，而 `addition`/`deletion` 有默认实现，正是这对默认值构成了下一节的「双开关」：[calculus.h:19-28](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/calculus.h#L19-L28)。

```cpp
class Calculation {
 public:
  using Factory = Calculation*(const vector<string>& args);
  virtual bool Apply(Spelling* spelling) = 0;
  virtual bool addition() { return true; }
  virtual bool deletion() { return true; }
};
```

- `Factory` 是一个函数指针类型，签名固定为「接收参数数组，返回 `Calculation*`」。每个子类都提供一个静态 `Parse` 方法，签名与它一致。
- `addition()`/`deletion()` 的含义留到 4.4 节统一讲，这里只需记住：基类默认两者皆为 `true`。

`Calculus` 本身极简，一张 `map` 存工厂：[calculus.h:30-38](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/calculus.h#L30-L38)。构造函数里把六个内置运算一次性注册：[calculus.cc:18-25](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/calculus.cc#L18-L25)。

```cpp
Calculus::Calculus() {
  Register("xlit",   &Transliteration::Parse);
  Register("xform",  &Transformation::Parse);
  Register("erase",  &Erasion::Parse);
  Register("derive", &Derivation::Parse);
  Register("fuzz",   &Fuzzing::Parse);
  Register("abbrev", &Abbreviation::Parse);
}
```

注意：注册表里**只有六个名字**。`Correction` 没有自己的运算名——它只能通过 `derive/.../correction` 这种「带 tag 的 derive」间接产生（见 4.3）。所以 `Calculus` 对外只认 `xlit/xform/erase/derive/fuzz/abbrev` 这六个 token。

#### 4.1.4 代码实践

**目标**：确认「六个运算名」与「工厂表」的对应关系。

1. 打开 [calculus.cc:18-25](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/calculus.cc#L18-L25)。
2. 数清楚 `Register` 调用的次数与名字。
3. 在 [calculus.h:41-97](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/calculus.h#L41-L97) 里找到 `Correction` 类，观察它**只有 `Apply`、没有 `static Parse`**。

**预期结果**：你会确认 `Correction` 没有被注册，也没有 `Parse`，它只能由 `Derivation::Parse` 内部分发创建。

#### 4.1.5 小练习与答案

**练习 1**：如果要新增一个运算名 `foo`，最少要改哪几处？

**答案**：写一个 `Calculation` 子类并提供静态 `Parse`；在 `Calculus::Calculus()` 里加一行 `Register("foo", &Foo::Parse)`。不需要改 `Parse` 的主流程——它是数据驱动的。

**练习 2**：`Calculation::Factory` 为什么是函数指针而不是 `std::function`？

**答案**：这些工厂都是无状态的静态成员函数，函数指针足够、零开销，也便于在构造期用 `&Class::Parse` 取地址注册。

### 4.2 运算定义串的解析：分隔符魔法

#### 4.2.1 概念说明

公式串的语法是 `运算名<分隔符>参数1<分隔符>参数2<分隔符>...<分隔符>`，例如 `xform/x/y/` 或 `xlit|abc|xyz|`。最巧妙的地方是：**分隔符不是固定的 `/`，而是「串里第一个不是小写字母的字符」**。

为什么要这样设计？因为参数里经常是正则表达式，而正则本身常含 `/`。允许用户自选分隔符（`/`、`|` 甚至别的），就能避免把 `/` 写成 `\/` 那样的丑陋转义。本讲后面会看到 cangjie 方案用 `|` 作分隔符的真实例子。

#### 4.2.2 核心流程

```
Parse("xform/x/y/")
  1. find_first_not_of("a..z") -> 找到第一个非小写字母字符 = '/'，位置 5
  2. 以该字符为分隔符 boost::split -> args = ["xform", "x", "y", ""]
  3. args[0]="xform" 查 factories_ -> 命中 Transformation::Parse
  4. 调用 Transformation::Parse(args) -> 用 args[1]=pattern, args[2]=replacement 构造对象
```

#### 4.2.3 源码精读

分隔符的判定只有一行，但非常关键：[calculus.cc:31-45](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/calculus.cc#L31-L45)。

```cpp
Calculation* Calculus::Parse(const string& definition) {
  size_t sep = definition.find_first_not_of("zyxwvutsrqponmlkjihgfedcba");
  if (sep == string::npos)
    return NULL;
  vector<string> args;
  boost::split(args, definition,
               boost::is_from_range(definition[sep], definition[sep]));
  // ... 用 args[0] 查 factories_ 并调用工厂
}
```

- 那个看起来倒着写的字符串 `"zyxwvutsrqponmlkjihgfedcba"` 其实就是 26 个小写字母的集合（顺序无所谓，`find_first_not_of` 只看成员资格）。运算名 `xform`/`derive` 等全由小写字母组成，所以扫过运算名后遇到的第一个「非小写字母」必然是分隔符。
- 若整串都是小写字母（比如只有 `"xform"` 没带分隔符），返回 `NULL` 表示解析失败。
- `boost::is_from_range(c, c)` 构造一个「只匹配字符 c」的谓词，于是 `boost::split` 就用这个分隔符把串切开。**末尾的分隔符会产生一个空字符串元素**，所以 `xform/x/y/` 切出来是 4 段（最后一段为空）。

每个子类的 `Parse` 再从 `args` 里取自己需要的字段。以 `Transformation::Parse` 为例，它要 pattern 和 replacement 两段：[calculus.cc:99-110](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/calculus.cc#L99-L110)。

```cpp
Calculation* Transformation::Parse(const vector<string>& args) {
  if (args.size() < 3) return NULL;
  const string& left(args[1]);    // 正则
  const string& right(args[2]);   // 替换串
  if (left.empty()) return NULL;
  // 构造对象，赋值 pattern_ / replacement_
}
```

而 `Erasion::Parse` 只要一段正则（`erase/^xx$/`），所以只检查 `args.size() < 2`：[calculus.cc:124-133](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/calculus.cc#L124-L133)。

特别地，`Derivation::Parse` 会**偷看第四段 `args[3]`** 来决定真正要造哪种子类：[calculus.cc:146-185](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/calculus.cc#L146-L185)。这一段就是 `derive/^nve$/nue/correction` 里最后的 `correction`。

```cpp
if (args.size() > 3) {
  const string& tag = args[3];
  if (tag == "correction") { /* 造 Correction */ }
  if (tag == "abbrev")     { /* 造 Abbreviation */ }
  if (tag == "fuzz")       { /* 造 Fuzzing */ }
}
// tag 为空或无法识别，造普通 Derivation
```

这意味着同一个 `derive` 运算名，依据第四段 tag 能产生四种不同对象。这解释了为什么 `fuzz`、`abbrev` 既有自己的运算名（`fuzz/...`、`abbrev/...`），又能写成 `derive/.../fuzz`、`derive/.../abbrev`——两条路径殊途同归。

#### 4.2.4 代码实践

**目标**：亲手验证「分隔符自选」与「末尾空段」。

1. 阅读 [calculus.cc:31-45](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/calculus.cc#L31-L45) 的 `Parse`。
2. 在纸上模拟 `Parse("xlit|abcdefghijklmnopqrstuvwxyz|日月金木|")`：第一个非小写字母字符是 `|`（位置 4），切出来应为 `["xlit", "abcdefghijklmnopqrstuvwxyz", "日月金木...", ""]`。
3. 到 [luna_pinyin.schema.yaml:108](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.schema.yaml#L108) 核对 cangjie 的 `preedit_format` 确实用了 `|` 分隔符。

**预期结果**：因为参数里不含小写字母冲突，`|` 被正确识别为分隔符，`xlit` 工厂拿到左右两段字符表。**待本地验证**：若你写一个最小测试调用 `Calculus::Parse`，可断言返回非空。

#### 4.2.5 小练习与答案

**练习 1**：`Parse("xform/x/y/")` 切出的 `args` 有几个元素？最后一个是什么？

**答案**：4 个：`["xform", "x", "y", ""]`。最后一个是由末尾 `/` 产生的空串。

**练习 2**：为什么运算名必须全由小写字母组成？

**答案**：`find_first_not_of` 把「第一个非小写字母字符」当作分隔符，运算名本身必须全部落在小写字母集合内，分隔符才不会被误判进运算名里。

### 4.3 六类运算的语义、继承体系与属性副作用

#### 4.3.1 概念说明

六类运算可按「对拼写做了什么」分成三组：

| 组 | 运算 | 行为 | addition/deletion |
|----|------|------|-------------------|
| 替换 | `xform` | 正则替换，原拼写被新拼写取代 | true / true |
| 删除 | `erase` | 整串匹配则清空，候选消失 | **false** / true |
| 增补 | `derive`/`fuzz`/`abbrev`/`correction` | 派生一条新拼写，**原拼写保留** | true / **false** |

这里的 `addition`/`deletion` 就是 4.1 节那对虚函数的取值，它决定了「原拼写要不要留、新拼写要不要加」，是 4.4 节 `Projection` 统一调度的关键。本节先记住：**替换类**两值都真、**删除类** `addition=false`、**增补类** `deletion=false`。

#### 4.3.2 核心流程：继承体系

```
Calculation
├── Transliteration     xlit   逐字符映射
├── Transformation      xform  正则替换（替换类）
│   └── Derivation      derive 正则替换，但保留原拼写（增补类）
│       ├── Fuzzing        fuzz      + 置 type=kFuzzySpelling,    credibility += log(0.5)
│       ├── Abbreviation   abbrev    + 置 type=kAbbreviation,     credibility += log(0.5)
│       └── Correction     (derive/.../correction)  + 置 is_correction, credibility += log(0.01)
└── Erasion             erase  正则全匹配则清空（删除类）
```

注意三条要点：

1. `Derivation` 继承 `Transformation`，复用了「正则替换」的 `pattern_`/`replacement_` 与 `Apply` 主体，只是把 `deletion()` 改成 `false`——「派生」本质就是「替换但原件不删」。
2. `Erasion` **不**继承 `Transformation`：它用的是 `regex_match`（整串匹配）而非 `regex_replace`（替换），匹配成功就把 `str` 清空。
3. `Fuzzing`/`Abbreviation`/`Correction` 的 `Apply` 都先调用 `Transformation::Apply` 完成替换，再各自叠加不同的属性副作用。

#### 4.3.3 源码精读

**替换类 `Transformation::Apply`**：用 `boost::regex_replace`，若结果与原串相同则返回 `false`（未改变）：[calculus.cc:112-120](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/calculus.cc#L112-L120)。

```cpp
bool Transformation::Apply(Spelling* spelling) {
  string result = boost::regex_replace(spelling->str, pattern_, replacement_);
  if (result == spelling->str) return false;  // 没变
  spelling->str.swap(result);
  return true;
}
```

**删除类 `Erasion::Apply`**：用 `regex_match` 做整串匹配，命中即 `clear()`，并把 `addition()` 改写为 `false`：[calculus.cc:135-142](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/calculus.cc#L135-L142) 与 [calculus.h:62-70](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/calculus.h#L62-L70)。

```cpp
bool Erasion::Apply(Spelling* spelling) {
  if (!boost::regex_match(spelling->str, pattern_)) return false;  // 整串匹配
  spelling->str.clear();
  return true;
}
bool addition() override { return false; }  // 删除类：不产生新增
```

**增补类的属性副作用**：三者都先借用 `Transformation::Apply` 做替换，再叠加惩罚。惩罚常数定义在文件顶部：[calculus.cc:14-16](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/calculus.cc#L14-L16)。

```cpp
const double kAbbreviationPenalty = -0.6931471805599453;  // log(0.5)
const double kFuzzySpellingPenalty = -0.6931471805599453;  // log(0.5)
const double kCorrectionPenalty   = -4.605170185988091;    // log(0.01)
```

以 `Fuzzing::Apply` 为例：[calculus.cc:202-209](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/calculus.cc#L202-L209)。

```cpp
bool Fuzzing::Apply(Spelling* spelling) {
  bool result = Transformation::Apply(spelling);   // 先替换
  if (result) {
    spelling->properties.type = kFuzzySpelling;    // 再打标
    spelling->properties.credibility += kFuzzySpellingPenalty;
  }
  return result;
}
```

`Abbreviation::Apply`（[calculus.cc:226-233](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/calculus.cc#L226-L233)）把 `type` 置为 `kAbbreviation`、加同样的 `log(0.5)`；`Correction::Apply`（[calculus.cc:237-244](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/calculus.cc#L237-L244)）则置 `is_correction=true` 并加更重的 `log(0.01)`。

**`xlit` 逐字符映射**：`Transliteration::Apply` 用一张 `map<uint32_t,uint32_t>` 把拼写里每个 Unicode 码点逐一替换，常用于把拉丁字母 `a..z` 显示成仓颉字根 `日月金木…`：[calculus.cc:70-95](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/calculus.cc#L70-L95)。它的 `Parse` 要求左右两段字符表的码点数严格相等：[calculus.cc:49-68](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/calculus.cc#L49-L68)。

> 对数惩罚的直觉：规范化拼写 `credibility = 0`（即 `log(1)`，概率 1）。模糊/缩写打 `log(0.5)` 折半，纠错打 `log(0.01)` 只剩 1%。概率相乘在对数空间是相加：一条拼写若同时是模糊又是缩写，可信度会累加为 `log(0.5)+log(0.5)=log(0.25)`。用数学式表达：
>
> \[ \text{cred}(\text{复合}) = \sum_i \log p_i = \log\!\left(\prod_i p_i\right) \]

#### 4.3.4 代码实践（本讲指定任务）

**目标**：对照 [luna_pinyin.schema.yaml:74-86](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.schema.yaml#L74-L86) 的 `speller/algebra`，逐条标注它属于哪类运算。

1. 打开上面链接的 13 条规则。
2. 按运算名（`args[0]`）与第四段 tag 分类，填入下表。

**分类结果**：

| # | 规则 | 运算名 | 第四段 tag | C++ 类 | 组 |
|---|------|--------|-----------|--------|-----|
| 1 | `erase/^xx$/` | erase | — | Erasion | 删除 |
| 2 | `abbrev/^([a-z]).+$/$1/` | abbrev | — | Abbreviation | 增补 |
| 3 | `abbrev/^([zcs]h).+$/$1/` | abbrev | — | Abbreviation | 增补 |
| 4 | `derive/^([nl])ve$/$1ue/correction` | derive | correction | Correction | 增补（纠错） |
| 5 | `derive/^([jqxy])u/$1v/correction` | derive | correction | Correction | 增补（纠错） |
| 6 | `derive/un$/uen/correction` | derive | correction | Correction | 增补（纠错） |
| 7 | `derive/ui$/uei/correction` | derive | correction | Correction | 增补（纠错） |
| 8 | `derive/iu$/iou/correction` | derive | correction | Correction | 增补（纠错） |
| 9 | `derive/([aeiou])ng$/$1gn/correction` | derive | correction | Correction | 增补（纠错） |
| 10 | `derive/([dtngkhrzcs])o(u\|ng)$/$1o/correction` | derive | correction | Correction | 增补（纠错） |
| 11 | `derive/ong$/on/correction` | derive | correction | Correction | 增补（纠错） |
| 12 | `derive/ao$/oa/correction` | derive | correction | Correction | 增补（纠错） |
| 13 | `derive/([iu])a(o\|ng?)$/a$1$2/correction` | derive | correction | Correction | 增补（纠错） |

**观察**：

- 这份方案里**没有一条「裸 `derive`」**（即不带 tag 的普通派生），所有 `derive` 都是纠错。
- 第 1 条 `erase/^xx$/` 删掉的是 RIME 词典里的占位码 `xx`（无读音条目，如标点），让它不参与音节索引。
- 第 4 条以 `lve` 为例：词典里「略」记作 `lve`（`v=ü`），规则 `^([nl])ve$ → $1ue` 把它替换成 `lue` 作为纠错变体，于是用户敲 `lue` 也能命中。可在词典里用 `grep lve` 验证规范形式确实是 `lve`。

**预期结果**：13 条 = 1 删除 + 2 缩写 + 10 纠错，分类与上表一致。

#### 4.3.5 小练习与答案

**练习 1**：`fuzz/zh/z/` 和 `derive/zh/z/fuzz` 有区别吗？

**答案**：没有。前者直接走 `Fuzzing::Parse`；后者在 `Derivation::Parse` 里识别到 tag `fuzz` 也造一个 `Fuzzing`（[calculus.cc:172-177](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/calculus.cc#L172-L177)）。两者 `pattern_`/`replacement_` 相同，行为完全一致。

**练习 2**：为什么 `Erasion` 用 `regex_match` 而 `Transformation` 用 `regex_replace`？

**答案**：`erase` 的语义是「整串命中就删除」，需要全串匹配，故用 `regex_match`；`xform` 是「找到子串就替换」，用 `regex_replace`。前者还要 `addition()=false` 防止把清空后的空串当作新拼写加回去。

### 4.4 Projection 与 Script：批量化、双开关与两种应用方式

#### 4.4.1 概念说明

`Calculation` 只处理「一条拼写」。真实需求是「对整本音节表依次施加一串规则」。`Projection` 就是这层批量外壳：它持有一个 `vector<of<Calculation>> calculation_`（一串算子），提供两种 `Apply`：

- `Apply(string*)`：把**单个字符串**当拼写，顺序套用所有算子——用于显示期格式化（如 preedit 里把 `v` 显示成 `ü`）。
- `Apply(Script*)`：对**整张拼写表**的每个音节套用所有算子——用于构建期派生（展开模糊音/缩写/纠错变体入索引）。

`Script` 则是「拼写表」：`map<string, vector<Spelling>>`，键是拼写串，值是该拼写所有变体的属性列表（一条规范拼写 + 若干派生变体）。

#### 4.4.2 核心流程

**`Projection::Apply(Script*)` 的双开关循环**（本节核心）：

```
对 calculation_ 里的每个算子 x（一轮一换）：
  建一张空 temp Script
  对当前 Script 里的每个拼写 v：
    s = v 的副本；applied = x->Apply(&s)
    若 applied：
       若 x->deletion() == false：把原拼写 v 原样 Merge 进 temp     # 增补类：保留原件
       若 x->addition() == true 且 s.str 非空：把派生拼写 s Merge 进 temp  # 替换/增补类：加入新件
    否则（未命中）：
       把原拼写 v 原样 Merge 进 temp                                  # 规则没动它
  用 temp 替换当前 Script
```

三种语义就这样被同一套循环覆盖：

- **替换（xform）** `deletion=true, addition=true`：原件不留、新件加入 ⇒ 原拼写被改写。
- **删除（erase）** `deletion=true, addition=false`：原件不留、新件（空串）也不加 ⇒ 候选消失。
- **增补（derive/fuzz/abbrev/correction）** `deletion=false, addition=true`：原件保留、新件也加 ⇒ 多出一条变体。

#### 4.4.3 源码精读

`Script` 的数据结构继承自 `map`：[algebra.h:20-27](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/algebra.h#L20-L27)。`AddSyllable` 加入规范拼写（`type=kNormal`、`credibility=0`）：[algebra.cc:14-20](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/algebra.cc#L14-L20)。`Merge` 负责把派生拼写并入：先 `Compose` 叠加属性，再按 `str` 去重（`operator==` 只比串），命中则 `Update` 合并属性、未命中则 push：[algebra.cc:22-38](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/algebra.cc#L22-L38)（`Compose`/`Update` 的细节见 u7-l1）。

`Projection::Load` 把 YAML 里的规则列表编译成算子序列。注意它在栈上**临时**建一个 `Calculus`，逐条 `Parse`，任何一条失败就清空全部并返回 `false`：[algebra.cc:54-86](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/algebra.cc#L54-L86)。

```cpp
bool Projection::Load(an<ConfigList> settings) {
  Calculus calc;
  for (size_t i = 0; i < settings->size(); ++i) {
    an<ConfigValue> v(settings->GetValueAt(i));
    an<Calculation> x;
    try { x.reset(calc.Parse(v->str())); }
    catch (boost::regex_error& e) { LOG(ERROR) << ...; }
    if (!x) { success = false; break; }
    calculation_.push_back(x);
  }
  if (!success) calculation_.clear();
  return success;
}
```

**两种 Apply 的对照**：

字符串版把字符串包成临时 `Spelling`，顺序套用算子，最后写回。它只看「有没有被改」，不区分 addition/deletion（因为单个字符串没有「保留原件」的概念）：[algebra.cc:88-105](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/algebra.cc#L88-L105)。

Script 版才是双开关真正发挥作用的地方：[algebra.cc:107-140](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/algebra.cc#L107-L140)。

```cpp
for (an<Calculation>& x : calculation_) {        // 每个算子一轮
  Script temp;
  for (const Script::value_type& v : *value) {   // 遍历每个拼写
    Spelling s(v.first);
    bool applied = x->Apply(&s);
    if (applied) {
      modified = true;
      if (!x->deletion())                         // 增补类：保留原件
        temp.Merge(v.first, SpellingProperties(), v.second);
      if (x->addition() && !s.str.empty())        // 替换/增补类：加入新件
        temp.Merge(s.str, s.properties, v.second);
    } else {
      temp.Merge(v.first, SpellingProperties(), v.second);  // 未命中：原样保留
    }
  }
  value->swap(temp);                              // 一轮一换
}
```

**两种模式的真实调用方**：

- 构建期（Script 版）：`DictCompiler::BuildPrism` 先把音节表灌进 `Script`，再 `p.Apply(&script)` 展开，最后喂给 `Prism::Build`：[dict_compiler.cc:310-325](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc#L310-L325)。
- 显示期（字符串版）：`TranslatorOptions` 持有两个 `Projection`——`preedit_formatter_` 与 `comment_formatter_`，分别从方案的 `preedit_format`/`comment_format` 加载：[translator_commons.cc:133-136](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/translator_commons.cc#L133-L136)，并在显示前对单个字符串调用 `Apply`，例如 [script_translator.cc:304](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/script_translator.cc#L304) 的 `preedit_formatter_.Apply(&result)`、[table_translator.cc:50](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/table_translator.cc#L50)。

同一条 `xform/([nl])v/$1ü/`（见 [luna_pinyin.schema.yaml:90-91](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.schema.yaml#L90-L91) 的 `preedit_format`）就是字符串版的应用：它把 preedit 里的 `nv` 显示成 `nü`，**只改显示、不改索引**。

`Script::Dump` 是个调试宝贝，它把每条拼写的 `type` 编码成单字符输出：[algebra.cc:40-52](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/algebra.cc#L40-L52)，索引串 `"-ac?!"` 对应 `kNormal='-'`、`kFuzzy='a'`、`kAbbrev='c'`、`kCompletion='?'`、`kAmbiguous='!'`。

#### 4.4.4 代码实践

**目标**：在纸上跑一次 `Projection::Apply(Script*)`，验证双开关的「替换/删除/增补」三分。

设初始音节表 `Script = {lve, zhi}`（均已 `AddSyllable`，规范拼写 `type=kNormal, credibility=0`），算子序列为：

```
[ derive/^([nl])ve$/$1ue/correction,   // Correction
  abbrev/^([zcs]h).+$/$1/ ]            // Abbreviation
```

**第 1 轮（Correction）**：

- `lve`：命中 `^([nl])ve$`，替换为 `lue`，`is_correction=true`，`credibility += log(0.01) = -4.605`。`deletion=false` ⇒ 保留 `lve`；`addition=true` ⇒ 加入 `lue(correction, -4.605)`。
- `zhi`：不命中 ⇒ 原样保留。
- 结果：`{lve[normal/0], lue[correction/-4.605], zhi[normal/0]}`

**第 2 轮（Abbreviation）**：

- `lve`/`lue`：不命中 `^([zcs]h).+$` ⇒ 原样保留。
- `zhi`：命中（`group1=zh`），替换为 `zh`，`type=kAbbreviation`，`credibility += log(0.5) = -0.693`。`deletion=false` ⇒ 保留 `zhi`；`addition=true` ⇒ 加入 `zh(abbrev, -0.693)`。
- 最终：`{lve[normal/0], lue[correction/-4.605], zhi[normal/0], zh[abbrev/-0.693]}`

**预期结果**：构建后的 `Prism` 会接受四种输入形式 `lve / lue / zhi / zh`，其中 `lue` 是纠错（可信度最低）、`zh` 是缩写（可信度折半）。

**待本地验证**：可在测试里构造这段 `Script` 与 `Projection`，调用 `Apply` 后用 `Script::Dump` 打印，核对 `type` 字符与 `credibility` 数值。

#### 4.4.5 小练习与答案

**练习 1**：若把上面第 1 轮的 `derive` 换成 `xform`（替换类），`lve` 的命运会怎样？

**答案**：`xform` 的 `deletion=true`，所以 `lve` 不会被保留，只会变成 `lue`。最终表里不再有 `lve`——这与「纠错让两种写法都可用」的初衷相悖，故纠错必须用 `derive/correction` 而非 `xform`。

**练习 2**：`Projection::Load` 里 `Calculus calc;` 为什么可以建在栈上、用完即弃？

**答案**：`Calculus` 只在 `Parse` 期间被用来「编译」公式串成 `Calculation` 对象；一旦对象存进 `calculation_`，就与 `Calculus` 无关（`Calculation` 自带 `pattern_` 等状态）。所以 `Calculus` 是个纯编译器，无需长存。

## 5. 综合实践

把本讲的知识串起来，完成一个「迷你音节展开器」的源码阅读 + 手算任务。

**任务背景**：你要为一个极简拼音方案设计 `speller/algebra`，初始音节表只有 `{lve, zhi, xx}`（`xx` 是占位码）。希望达到：

1. 占位码 `xx` 不进入索引。
2. `lve` 允许纠错成 `lue`。
3. `zhi` 允许缩写成 `zh`。
4. preedit 里把 `nv` 显示成 `nü`。

**操作步骤**：

1. **写规则**：参照 [luna_pinyin.schema.yaml:73-86](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.schema.yaml#L73-L86) 与 [:90-91](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.schema.yaml#L90-L91)，写出对应的 `algebra` 与 `preedit_format`。
2. **追踪解析**：对每条规则，说明它会经过 [Calculus::Parse](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/calculus.cc#L31-L45) 切成什么样的 `args`，再由哪个子类的 `Parse` 造出哪个 C++ 类。
3. **追踪展开**：按 4.4.4 的方法，手算 `Projection::Apply(Script*)` 跑完后的最终 `Script`，列出每个键及其变体的 `type`/`credibility`。
4. **区分两种 Apply**：说明 1-3 用的是 `Apply(Script*)`（构建期，经 [dict_compiler.cc:318-325](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc#L318-L325)），而第 4 条 preedit 用的却是 `Apply(string*)`（显示期，经 [translator_commons.cc:133-136](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/translator_commons.cc#L133-L136)），两者共享同一套 `Calculus` 但效果不同。

**参考规则**：

```yaml
speller:
  algebra:
    - erase/^xx$/
    - derive/^([nl])ve$/$1ue/correction
    - abbrev/^([zcs]h).+$/$1/
translator:
  preedit_format:
    - xform/([nl])v/$1ü/
```

**预期最终 `Script`**：`{lve[normal/0], lue[correction/-4.605], zhi[normal/0], zh[abbrev/-0.693]}`，`xx` 被删除。preedit 显示时 `nv→nü` 仅作用于展示串，不影响索引。

## 6. 本讲小结

- `Calculus` 是「运算名 → 工厂函数」的注册表，内置六个运算名 `xlit/xform/erase/derive/fuzz/abbrev`；`Correction` 没有自己的运算名，只能由 `derive/.../correction` 间接产生。
- 公式串的分隔符是「串里第一个非小写字母字符」，故可用 `/`、`|` 等任意分隔符，避免正则里 `/` 的转义；末尾分隔符会产生一个空段。
- 六类运算构成一棵继承树：`Transformation`（替换）派生 `Derivation`（增补），再派生 `Fuzzing/Abbreviation/Correction`；`Erasion`（删除）独立，用 `regex_match` 全串匹配后清空。
- `addition()`/`deletion()` 这对虚函数是统一调度核心：替换类（真/真）、删除类（假/真）、增补类（真/假），让 `Projection::Apply(Script*)` 的一套循环同时表达替换、删除、增补三种行为。
- `Projection` 有两种 `Apply`：`Apply(Script*)` 在构建期展开音节变体入 `Prism`，`Apply(string*)` 在显示期格式化 preedit/comment；两者共用同一套 `Calculus` 与公式语法。
- 惩罚常数在对数空间：模糊/缩写 `log(0.5)`、纠错 `log(0.01)`，相乘的概率退化为 `credibility` 相加。

## 7. 下一步学习建议

- **u7-l3 Syllabifier 与音节图**：本讲的 `Script` 是「规范音节展开后的拼写表」，而 `Syllabifier` 解决的是「用户实际敲的输入串如何切分成音节序列并匹配到 `Prism`」。建议接着读 `src/rime/algo/syllabifier.h/.cc`，理解 `Prism`（u8-l2）如何消费本讲产出的拼写索引。
- **u8-l1/u8-l2 词典系统与 Prism**：去看 [dict_compiler.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc) 与 `src/rime/dict/prism.cc`，确认本讲的 `Projection::Apply(Script*)` 产物是如何被建成双数组 trie 的。
- **动手扩展**：仿照 4.1.5 的思路，试着注册一个自定义运算名（例如 `strip` 去掉某后缀），体会 `Calculus` 数据驱动注册的可扩展性。
