# Encoder：编码生成

## 1. 本讲目标

本讲拆解 librime 词典构建期的「编码生成器」（Encoder）。读完本讲你应当能够：

- 说清 `RawCode`、`PhraseCollector`、`Encoder` 三个抽象各自的角色，以及它们如何把「短语」变成「可入库的编码」。
- 读懂形码方案里 `encoder/rules` 的公式 DSL（如 `AaAzBaBbBz`），能把公式里的每一个字母对翻译成「第几个字的第几个码」。
- 手动追踪 `TableEncoder` 如何按公式从一个多字词的逐字编码里「取码」拼出最终编码，并理解 `tail_anchor`（尾锚）对仓颉复合字取码的作用。
- 区分两套 DFS：`TableEncoder`（形码，逐字取码 + 套公式）与 `ScriptEncoder`（音码，按词切分 + 直接拼接音节）。
- 看懂 `EntryCollector` 如何根据 `use_rule_based_encoder()` 选择编码器，并在「三遍收集」的第二遍（Pass 2）把「没有写码的词条」送进编码器自动补码。

本讲承接 [u8-l4 DictCompiler 构建流程](u8-l4-dict-compiler.md)：那一讲里 `EntryCollector` 的「Pass 2 给无码词自动编码」曾被当成黑盒，本讲正是打开这个黑盒。

## 2. 前置知识

- **音码与形码**：音码（如拼音）以「发音音节」为编码单位，一个字对应一串音节；形码（如仓颉、五笔）以「字根拆分」为编码单位，一个字对应一串字根字母。两种方案对「词组编码」的需求完全不同，这就是本讲存在两套编码器的根本原因。
- **词条与编码**：在 `*.dict.yaml` 里，一行词条形如 `你好\tni hao`，`ni hao` 就是「你好」的编码（用空格分隔的两个音节）。但有些词条**只写了文字没写编码**（如词组库），这时就需要 Encoder 根据单字编码把它们「推导」出来。
- **RawCode 与音节**：librime 内部把一条编码存成一个字符串数组（每个元素是一个音节或字根串），这就是下面要讲的 `RawCode`。
- **DFS（深度优先搜索）**：本讲两套编码器都用 DFS 遍历短语的切分方式，需要基本的递归回溯直觉。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `src/rime/algo/encoder.h` | Encoder 体系的全部声明：`RawCode`、`PhraseCollector`、`Encoder` 基类、`CodeCoords`/`TableEncodingRule`、`TableEncoder`、`ScriptEncoder`。 |
| `src/rime/algo/encoder.cc` | 上述类的实现，重点是公式解析 `ParseFormula`、取码算法 `Encode`/`CalculateCodeIndex`、两套 `DfsEncode`。 |
| `src/rime/dict/entry_collector.h` | `EntryCollector` 继承 `PhraseCollector`，持有 `the<Encoder> encoder`，是编码器的实际使用者。 |
| `src/rime/dict/entry_collector.cc` | `Configure` 选编码器、`Finish` 调度 Pass 2/Pass 3 编码、`TranslateWord` 提供单字编码。 |
| `src/rime/dict/dict_settings.cc` | `use_rule_based_encoder()` 读取 `encoder/rules` 判断是否走规则编码。 |
| `data/minimal/cangjie5.dict.yaml` | 真实的仓颉形码方案，文件头含一段完整的 `encoder` 配置，是本讲最好的现实样例。 |
| `test/encoder_test.cc` | 编码器的单元测试，覆盖公式解析、排除模式、取码、尾锚，是本讲代码实践的基础。 |

## 4. 核心概念与源码讲解

### 4.1 数据管线三件套：RawCode / PhraseCollector / Encoder

#### 4.1.1 概念说明

「给一个短语生成编码」这件事，拆开来其实是三个互相独立的职责：

1. **编码的数据形态**——一条编码在内存里长什么样？答案是 `RawCode`：一个字符串数组，每个元素是一个音节或字根串。
2. **编码的归宿**——算出来的编码要交给谁去入库？答案是 `PhraseCollector`（短语收集器）这个回调接口。
3. **编码的算法**——怎么从短语算出编码？答案才是 `Encoder` 基类及其两个子类。

把这三者分离开，编码器就只关心「算」，不关心「存」；而「存」的逻辑（写进 `entries`、去重、学新音节）全部留在 `EntryCollector` 里。这是一种典型的**依赖倒置**：高层（收集器）定义接口，低层（编码器）只调用接口。

#### 4.1.2 核心流程

```
短语 "你好"
   │  Encoder.EncodePhrase("你好", weight)
   ▼
┌─────────────────────────────────────────┐
│  TableEncoder / ScriptEncoder           │
│  1. DfsEncode: 把短语切成字/词            │
│  2. 对每个字调 collector->TranslateWord  │
│     取回它的单字编码                      │
│  3. (TableEncoder) 套公式取码            │
│  4. collector->CreateEntry(短语, 编码, 权重) │
└─────────────────────────────────────────┘
   │
   ▼
EntryCollector 把 (text, code, weight) 存进 entries
```

注意箭头方向：编码器**调用**收集器（`CreateEntry` / `TranslateWord`），而不是收集器调用编码器。`EncodePhrase` 只是入口，真正的「写库」发生在回调里。

#### 4.1.3 源码精读

`RawCode` 就是 `vector<string>` 的子类，外加两个互逆的序列化方法：

```cpp
class RawCode : public vector<string> {
 public:
  RIME_DLL string ToString() const;
  RIME_DLL void FromString(const string& code_str);
};
```

[src/rime/algo/encoder.h:16-20](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/encoder.h#L16-L20) 定义了它。`ToString` 用空格把各音节拼回字符串，`FromString` 按空格切分——这与 `*.dict.yaml` 里 `ni hao` 的格式直接对应：

```cpp
string RawCode::ToString() const { return strings::join(*this, " "); }
void RawCode::FromString(const string& code_str) {
  *dynamic_cast<vector<string>*>(this) =
      strings::split(code_str, " ", strings::SplitBehavior::SkipToken);
}
```

[src/rime/algo/encoder.cc:18-25](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/encoder.cc#L18-L25) 是其实现。注意切分时 `SkipToken` 会跳过空段，所以多余空格不影响。

`PhraseCollector` 是两个纯虚函数的回调接口：

```cpp
class PhraseCollector {
 public:
  virtual void CreateEntry(const string& phrase,
                           const string& code_str,
                           const string& value) = 0;
  virtual bool TranslateWord(const string& word, vector<string>* code) = 0;
};
```

[src/rime/algo/encoder.h:22-32](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/encoder.h#L22-L32) 定义了它。两个方法的方向相反：`TranslateWord` 是编码器**向收集器要**单字编码（输入「你」，返回 `["ni"]` 或多音时的多个候选）；`CreateEntry` 是编码器**把成品推回**给收集器（短语「你好」+ 推导出的编码「ni hao」）。`EntryCollector` 正是实现了这个接口（`class EntryCollector : public PhraseCollector`，见 [src/rime/dict/entry_collector.h:36](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/entry_collector.h#L36)）。

`Encoder` 基类只持有收集器指针，定义一个纯虚 `EncodePhrase`：

```cpp
class Encoder {
 public:
  Encoder(PhraseCollector* collector) : collector_(collector) {}
  virtual bool LoadSettings(Config* config) { return false; }
  virtual bool EncodePhrase(const string& phrase, const string& value) = 0;
  ...
 protected:
  PhraseCollector* collector_;
};
```

[src/rime/algo/encoder.h:36-49](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/encoder.h#L36-L49)。`LoadSettings` 默认空实现返回 `false`，只有 `TableEncoder` 重写它去读公式——这暗示了「公式配置是形码专属」。

#### 4.1.4 代码实践

**目标**：在测试里复现「短语→回调」的调用路径，确认编码器确实通过 `PhraseCollector` 回写。

**操作步骤**：

1. 打开 [test/encoder_test.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/test/encoder_test.cc)，注意测试只构造了 `TableEncoder encoder;`（无参），此时 `collector_` 为 `NULL`，所以测试只能验证 `Encode`（直接取码），不能验证 `EncodePhrase`（会触发 `collector_->CreateEntry` 解引用空指针）。
2. 想观察回调，可仿照 `EntryCollector` 写一个最小的 `PhraseCollector` 实现：在 `TranslateWord` 里返回硬编码编码、在 `CreateEntry` 里打印。
3. 用 `cmake --build build --target rime_test`（或 `make test`）编译测试目标。

**需要观察的现象**：`Encode` 类用例能在不触碰 `collector_` 的情况下通过，证明「取码算法」与「回调入库」是解耦的。

**预期结果**：`RimeEncoderTest.Encode` 全部通过；若你写了带 `PhraseCollector` 的小程序，能看到 `CreateEntry` 被调用且参数正是「短语 + 推导编码」。

**待本地验证**：具体测试命令与输出依构建环境而定，请在本机执行后核对。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Encoder` 基类要把 `collector_` 设为裸指针而非智能指针？
**答案**：因为收集器（`EntryCollector`）的生命周期由外部（`DictCompiler`）管理，编码器只是「借用」它来回调，不持有所有权。用裸指针表达「不拥有」的借用关系，符合 librime 里 `the<T>`（独占）/ `an<T>`（共享）/ 裸指针（借用）的智能指针约定（见 [u1-l3 common.h](u1-l3-source-layout.md)）。

**练习 2**：`PhraseCollector::TranslateWord` 返回 `bool` 且把结果写进出参 `vector<string>* code`，这个 `bool` 的语义是什么？
**答案**：表示「这个字/词是否查到了编码」。查到返回 `true` 并填充 `code`（可能含多音字的多个候选）；没查到返回 `false`，调用方（DFS）就不沿这条分支继续。

---

### 4.2 编码公式 DSL：CodeCoords 与 TableEncodingRule

#### 4.2.1 概念说明

形码方案给词组编码，通常有一套固定「取码规则」。比如仓颉对**双字词**的规则是「取第一个字的首码、尾码，再取第二个字的首码、次码、尾码」。这种规则需要一个简洁、人能读写的方式来表达——这就是公式 DSL。

librime 用一对字母表示「取哪个字的哪个码」：

- 大写字母表示**字的位置**：`A` = 第 1 个字，`B` = 第 2 个字…… `Z` = 倒数第 1 个字。
- 小写字母表示**码的位置**：`a` = 第 1 个码，`b` = 第 2 个码…… `z` = 倒数第 1 个码。

于是 `Aa` = 第 1 个字的第 1 个码，`Az` = 第 1 个字的最后一个码，`Za` = 倒数第 1 个字的第 1 个码。源码头部的注释把这三条直接写明了：

```cpp
// Aa : code at index 0 for character at index 0
// Az : code at index -1 for character at index 0
// Za : code at index 0 for character at index -1
```

[src/rime/algo/encoder.h:51-57](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/encoder.h#L51-L57)。一条公式就是若干个这样的「字母对」首尾相连，如 `AaAzBaBbBz`。

#### 4.2.2 核心流程

公式里的字母到「索引值」的映射是一段分段函数。设大写字母 `C`、小写字母 `c`：

\[
\text{char\_index}(C) = 
\begin{cases}
C - \text{'A'} & \text{若 } C \le \text{'T'} \quad (\text{即 } 0,1,\dots,19) \\
C - \text{'Z'} - 1 & \text{若 } C \ge \text{'U'} \quad (\text{即 } -6,-5,\dots,-1)
\end{cases}
\]

\[
\text{code\_index}(c) = 
\begin{cases}
c - \text{'a'} & \text{若 } c \le \text{'t'} \quad (\text{即 } 0,1,\dots,19) \\
c - \text{'z'} - 1 & \text{若 } c \ge \text{'u'} \quad (\text{即 } -6,-5,\dots,-1)
\end{cases}
\]

所以 `A`~`T` 是正向索引（`A`=0），`U`~`Z` 是反向索引（`Z`=-1）；小写同理。**负索引在运行期会加上字数 `num_syllables` 转成实际位置**——这正是 `Za`（倒数第 1 个字）能自适应不同长度词组的关键。

每个字母对解析成一个 `CodeCoords{char_index, code_index}`；一条公式解析成一串 `CodeCoords`，再配上「适用字长范围」就构成一条 `TableEncodingRule`：

```cpp
struct CodeCoords {
  int char_index;
  int code_index;
};
struct TableEncodingRule {
  int min_word_length;
  int max_word_length;
  vector<CodeCoords> coords;
};
```

[src/rime/algo/encoder.h:54-63](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/encoder.h#L54-L63)。`min/max_word_length` 决定这条规则对几个字的词生效（`length_equal: 2` 会把两者都设成 2）。

#### 4.2.3 源码精读

公式解析在 `ParseFormula`：

```cpp
bool TableEncoder::ParseFormula(const string& formula, TableEncodingRule* rule) {
  if (formula.length() % 2 != 0) { ... return false; }   // 长度必须偶数
  for (auto it = formula.cbegin(), end = formula.cend(); it != end;) {
    CodeCoords c;
    if (*it < 'A' || *it > 'Z') { ... return false; }     // 大写字母：字索引
    c.char_index = (*it >= 'U') ? (*it - 'Z' - 1) : (*it - 'A');
    ++it;
    if (*it < 'a' || *it > 'z') { ... return false; }     // 小写字母：码索引
    c.code_index = (*it >= 'u') ? (*it - 'z' - 1) : (*it - 'a');
    ++it;
    rule->coords.push_back(c);
  }
  return true;
}
```

[src/rime/algo/encoder.cc:108-131](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/encoder.cc#L108-L131)。两个守卫保证公式合法：长度必须偶数（每个字母对占 2 字符）、每对必须「大写+小写」。

`LoadSettings` 把 YAML 里的 `length_equal` / `length_in_range` 翻译成 `min/max_word_length`，并维护一个 `max_phrase_length_` 优化上限：

```cpp
if (an<ConfigValue> value = rule->GetValue("length_equal")) {
  int length = 0;
  if (!value->GetInt(&length)) { LOG(ERROR) << "invalid length"; continue; }
  r.min_word_length = r.max_word_length = length;
  if (max_phrase_length_ < length) max_phrase_length_ = length;
} else if (auto range = As<ConfigList>(rule->Get("length_in_range"))) {
  ... // 取 range[0]/range[1] 作 min/max
}
```

[src/rime/algo/encoder.cc:65-87](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/encoder.cc#L65-L87)。`loaded_` 最终由「是否有至少一条规则」决定（[L104](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/encoder.cc#L104)）。

下面是真实的仓颉配置，三段公式分别覆盖 2 字、3 字、4~10 字词：

```yaml
encoder:
  exclude_patterns:
    - '^x.*$'
    - '^z.*$'
  rules:
    - length_equal: 2
      formula: "AaAzBaBbBz"
    - length_equal: 3
      formula: "AaAzBaBzCz"
    - length_in_range: [4, 10]
      formula: "AaBzCaYzZz"
  tail_anchor: "'"
```

[data/minimal/cangjie5.dict.yaml:30-41](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/cangjie5.dict.yaml#L30-L41)。解读双字词公式 `AaAzBaBbBz`：第 1 字首码(`Aa`)、第 1 字尾码(`Az`)、第 2 字首码(`Ba`)、第 2 字次码(`Bb`)、第 2 字尾码(`Bz`)——取码共 5 码。

#### 4.2.4 代码实践

**目标**：用 [test/encoder_test.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/test/encoder_test.cc) 的 `RimeEncoderTest.Settings` 验证公式解析结果。

**操作步骤**：

1. 阅读该用例：它构造了三条规则，公式依次是 `AaAzBaBz`、`AaBaCaBz`、`AaBaCaZz`。
2. 手算 `AaAzBaBz` 解析出的 4 个 `CodeCoords`：`(0,0)`、`(0,-1)`、`(1,0)`、`(1,-1)`。
3. 运行测试核对断言。

**需要观察的现象**：断言 `rules[0].coords[1] == (0,-1)`（即 `Az`）与 `rules[2].coords[3] == (-1,-1)`（即 `Zz`，字与码都是倒数第 1）应当成立。

**预期结果**：`EXPECT_EQ(-1, rules[0].coords[1].code_index)` 与 `EXPECT_EQ(-1, rules[2].coords[3].char_index)` 通过，与你的手算一致。

**待本地验证**：请在本机构建并运行 `RimeEncoderTest.Settings`。

#### 4.2.5 小练习与答案

**练习 1**：公式 `AaBzCaYzZz`（仓颉 4~10 字词规则）里 `Yz` 表示什么？
**答案**：`Y` 是倒数第 2 个字（`Y`-'Z'-1 = -2），`z` 是最后一个码。所以 `Yz` = 倒数第 2 个字的最后一个码。运行期 `char_index = -2 + num_syllables` 转成实际位置。

**练习 2**：为什么小写反向索引从 `u` 而不是 `s` 开始？（即为何中间有跳跃）
**答案**：因为正向 `a`~`t` 占了 0~19，反向要无缝衔接 -1 起步。设计者把字母表均分：`a`~`t`(20 个) 表正向 0~19，`u`~`z`(6 个) 表反向 -6~-1；`z`-'z'-1 = -1。大写同理。这样用 26 个字母同时表达正反两个方向的索引。

---

### 4.3 TableEncoder：从公式到取码

#### 4.3.1 概念说明

公式只是「处方」，真正「按方抓药」的是 `TableEncoder::Encode`。它的输入是一个 `RawCode`（每个字一串码），输出是按公式取出的若干字符拼接成的最终编码。难点在三个细节：

1. **负索引归零**：`Z`/`z` 这种负索引要先加上字数才能定位。
2. **同一字内的推进**：像 `AaAb`（第 1 字第 1、2 码）要在同一字内顺序推进，不能重复取同一个码。
3. **尾锚 `tail_anchor`**：仓颉的复合字编码形如 `a'pq`，`'` 之后是「被包含部分」的字根。取「尾码」时应取 `'` 之前的主码末位，而不是整个串的末位。

#### 4.3.2 核心流程

`Encode` 的主干是一个双层循环：外层遍历所有规则找到第一条「字长匹配」的规则，内层遍历该规则的每个 `CodeCoords` 取码：

```
对每条 rule:
  若 num_syllables 不在 [min,max] → 跳过
  result = ""
  对 rule.coords 里每个 current:
    1. 复制成 c，负 char_index 加上 num_syllables
    2. 越界(字索引) → 跳过这个 coord
    3. 若与上一个同字 → start_index 推进，避免重复取码
    4. c.code_index = CalculateCodeIndex(code[c.char_index], c.code_index, start)
    5. 越界(码索引) → 跳过
    6. result += code[c.char_index][c.code_index]
  若 result 非空 → 返回 true
返回 false
```

#### 4.3.3 源码精读

`Encode` 主体（节选关键守卫）：

```cpp
for (const CodeCoords& current : rule.coords) {
  CodeCoords c(current);
  if (c.char_index < 0) c.char_index += num_syllables;   // 负索引归位
  if (c.char_index >= num_syllables) continue;            // 'abc def' ~ 'Ca' 越界
  if (c.char_index < 0) continue;                         // 'abc def' ~ 'Xa' 越界
  ...
  int start_index = 0;
  if (c.char_index == encoded.char_index)                 // 与上次同字
    start_index = encoded.code_index + 1;                 // 从下一码开始
  c.code_index = CalculateCodeIndex(code[c.char_index], c.code_index, start_index);
  if (c.code_index >= (int)code[c.char_index].length()) continue;  // 'Ad' 越界
  ...
  *result += code[c.char_index][c.code_index];
  previous = current; encoded = c;
}
```

[src/rime/algo/encoder.cc:141-198](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/encoder.cc#L141-L198)。几个关键点：负 `char_index` 在 L153-154 加上字数转正；同字推进在 L167-169 防止 `AaAb` 取到同一个码；越界 `continue` 直接跳过该 coord（不报错，保证公式对短码也健壮）。

`CalculateCodeIndex` 把「虚拟索引」映射到字符串真实下标，并处理尾锚：

```cpp
int TableEncoder::CalculateCodeIndex(const string& code, int index, int start) {
  const int n = code.length();
  int k = 0;
  if (index < 0) {                                   // 从尾部数
    k = n - 1;
    size_t tail = code.find_first_of(tail_anchor_, start + 1);  // 找第一个尾锚
    if (tail != string::npos) k = (int)tail - 1;     // 尾码 = 锚前一位
    while (++index < 0)                              // 继续往前数
      while (--k >= 0 && tail_anchor_.find(code[k]) != string::npos) {}
  } else {                                           // 从头部数
    while (index-- > 0)
      while (++k < n && tail_anchor_.find(code[k]) != string::npos) {}
  }
  return k;
}
```

[src/rime/algo/encoder.cc:207-234](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/encoder.cc#L207-L234)。源码上方 [L200-206](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/encoder.cc#L200-L206) 的注释给了详尽例子，例如 `'ab|cd|ef|g' ~ '(Aa)Az' -> 'ab'`（`|` 代表尾锚）：取 `Az` 时先定位到第一个锚之前，得到 `'b'` 前一位即 `'b'`... 实际逻辑是 `k = tail - 1`，对 `ab'cd` 取 `Az` 时 `tail=2`，`k=1` 即 `'b'`。一句话：**尾锚让「尾码」停在主码段末尾，忽略被包含部分的字根**。

`IsCodeExcluded` 用正则把「不该作为词组取码来源」的单字编码剔除（如仓颉的 `x*`/`z*` 特殊码）：

```cpp
bool TableEncoder::IsCodeExcluded(const string& code) {
  for (const boost::regex& pattern : exclude_patterns_)
    if (boost::regex_match(code, pattern)) return true;
  return false;
}
```

[src/rime/algo/encoder.cc:133-139](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/encoder.cc#L133-L139)。它在 DFS 取单字编码时被调用，过滤掉特殊码字根。

手动验证（对应测试 `RimeEncoderTest.Encode` case 1）：`c0 = "abc def"`（即 `["abc","def"]`，2 字），公式 `AaAbBaBb`：

- `Aa` → `code[0][0]='a'`；`Ab` → 同字推进 `code[0][1]='b'`
- `Ba` → `code[1][0]='d'`；`Bb` → 同字推进 `code[1][1]='e'`
- 结果 `"abde"`，与 [test/encoder_test.cc:71-72](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/test/encoder_test.cc#L71-L72) 的 `EXPECT_EQ("abde", result)` 一致。

#### 4.3.4 代码实践

**目标**：手算并验证仓颉双字词公式 `AaAzBaBbBz` 的取码结果。

**操作步骤**：

1. 设想一个双字词，两个字的单字编码分别为 `code[0]="abcd"`、`code[1]="efgh"`（构造一个 `RawCode`）。
2. 按公式 `AaAzBaBbBz` 手算：`Aa`='a'、`Az`='d'（尾码）、`Ba`='e'、`Bb`='f'、`Bz`='h'（尾码）。
3. 写一个仿照 `RimeEncoderTest.Encode` 的小用例：`config["encoder"]["rules"][0]["length_equal"]=2; formula="AaAzBaBbBz";`，`RawCode c; c.FromString("abcd efgh");`，调用 `encoder.Encode(c, &result)`。
4. 运行并比对。

**需要观察的现象**：`result` 应为 `"adefh"`。

**预期结果**：`result == "adefh"`。这正对应仓颉「第 1 字首尾 + 第 2 字首次尾」的 5 码规则。

**待本地验证**：测试需在本机编译运行；若无构建环境，至少完成手算并理解每一步对应公式里的哪个字母对。

#### 4.3.5 小练习与答案

**练习 1**：若 `code[0]="ab"`（只有 2 码），公式 `AaAbAc`（要取第 1 字的第 1、2、3 码）会发生什么？
**答案**：`Aa`='a'、`Ab`='b'，到 `Ac` 时 `code_index` 计算出 2，越界（`>= length()`），该 coord 被 `continue` 跳过。最终 `result="ab"`，不会越界崩溃——`Encode` 对短码是健壮的（见 [L172-174](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/encoder.cc#L172-L174)）。

**练习 2**：尾锚 `tail_anchor: "'"` 对一个编码为 `a'pq` 的字，`Az`（尾码）取到的是哪个字符？
**答案**：取到 `'a'`... 更准确说：`CalculateCodeIndex` 先找到第一个 `'`（位置 1），`k = 1-1 = 0`，所以取 `code[0]='a'`。即尾码是主码段（`'` 之前）的最后一个字符，`'pq` 这段被包含部分被忽略。

---

### 4.4 两套 DFS 与 EntryCollector 装配闭环

#### 4.4.1 概念说明

`Encode` 只解决「已知每个字的码，怎么取码」。但词组编码的真正入口是 `EncodePhrase(短语, 权重)`——它要先**把短语切成字/词并查出各自的码**，再交给 `Encode`（形码）或直接拼接（音码）。这一步用 DFS 完成，而形码与音码的切法根本不同：

- **TableEncoder（形码）逐字切**：每次切 1 个 UTF-8 字符，查它的单字编码，凑齐 N 个字后套公式取码。结果是「短编码」。
- **ScriptEncoder（音码）逐词切**：每次尝试切 1~剩余全长的多种词长，查该词的音节编码，到达末尾后直接把音节序列拼起来。结果是「音节序列」。

#### 4.4.2 核心流程

两套 DFS 对比：

```
TableEncoder.DfsEncode(短语, pos, code, limit):     ScriptEncoder.DfsEncode(...):
  若 pos == 末尾:                                       若 pos == 末尾:
    encoded = Encode(*code)        // 套公式             CreateEntry(短语, code->ToString())  // 直接拼
    collector->CreateEntry(短语, encoded)               return
    return                                              对 k = 剩余长度 .. 1:           // 试各种词长
  取 1 个 UTF-8 字符 word                                  word = 短语[pos, pos+k)
  TranslateWord(word) → 多个候选码                         TranslateWord(word) → 候选音节
  对每个候选码:                                            对每个候选音节:
    code.push_back(码)                                       code.push_back(音节)
    DfsEncode(下一字)                                        DfsEncode(pos+k)
    code.pop_back()                                          code.pop_back()
```

两者都用 `kEncoderDfsLimit = 32` 限制搜索叶子总数（[src/rime/algo/encoder.cc:15](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/encoder.cc#L15)），避免多音字组合爆炸。

#### 4.4.3 源码精读

`TableEncoder::DfsEncode` 用 `utf8::unchecked::next` 每次推进恰好一个字符：

```cpp
const char* word_start = phrase.c_str() + start_pos;
const char* word_end = word_start;
utf8::unchecked::next(word_end);                  // 推进一个 UTF-8 字符
size_t word_len = word_end - word_start;
string word(word_start, word_len);
...
if (collector_->TranslateWord(word, &translations)) {
  for (const string& x : translations) {
    if (IsCodeExcluded(x)) continue;              // 过滤特殊码
    code->push_back(x);
    bool ok = DfsEncode(phrase, value, start_pos + word_len, code, limit);
    ret = ret || ok;
    code->pop_back();
    if (limit && *limit <= 0) return ret;         // 触达搜索上限
  }
}
```

[src/rime/algo/encoder.cc:247-290](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/encoder.cc#L247-L290)。到达末尾时（L252）调 `Encode(*code, &encoded)` 套公式，再 `collector_->CreateEntry(phrase, encoded, value)` 把成品推回收集器。

`ScriptEncoder::DfsEncode` 的关键差异在循环上界——它从「剩余全长」递减尝试：

```cpp
for (size_t k = phrase.length() - start_pos; k > 0; --k) {
  string word(phrase.substr(start_pos, k));        // 试 1..剩余全长 各种词长
  vector<string> translations;
  if (collector_->TranslateWord(word, &translations)) {
    for (const string& x : translations) {
      code->push_back(x);
      bool ok = DfsEncode(phrase, value, start_pos + k, code, limit);
      ...
    }
  }
}
```

[src/rime/algo/encoder.cc:305-334](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/encoder.cc#L305-L334)。注意它到达末尾时（L310-315）**不调 `Encode`、不套公式**，直接 `collector_->CreateEntry(phrase, code->ToString(), value)`——音码的「词组编码」就是各组成词音节的顺序拼接。

`EntryCollector::Configure` 根据方案头里的 `encoder/rules` 是否存在来二选一：

```cpp
if (settings->use_rule_based_encoder()) {
  encoder.reset(new TableEncoder(this));
} else {
  encoder.reset(new ScriptEncoder(this));
}
encoder->LoadSettings(settings);
```

[src/rime/dict/entry_collector.cc:25-36](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/entry_collector.cc#L25-L36)。判定函数极其简洁：

```cpp
bool DictSettings::use_rule_based_encoder() {
  return (*this)["encoder"]["rules"].IsList();
}
```

[src/rime/dict/dict_settings.cc:67-69](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_settings.cc#L67-L69)。即「方案头里写了 `encoder/rules` 列表 → 用形码编码器；否则用音码编码器」。注意 `this` 即 `EntryCollector` 自身被作为 `PhraseCollector*` 传入构造（`new TableEncoder(this)`），这就是回调闭环的接驳点。

编码发生在 `EntryCollector::Finish` 的 Pass 2（处理 `*.dict.yaml` 里**没写编码**的词条）：

```cpp
while (!encode_queue.empty()) {
  const auto& phrase(encode_queue.front().first);
  const auto& weight_str(encode_queue.front().second);
  if (!encoder->EncodePhrase(phrase, weight_str)) {
    LOG(ERROR) << "Encode failure: '" << phrase << "'.";
  }
  encode_queue.pop();
}
```

[src/rime/dict/entry_collector.cc:134-143](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/entry_collector.cc#L134-L143)。`encode_queue` 在 Pass 1（[L120](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/entry_collector.cc#L120)）由「空 code 列」的行填入。`TranslateWord` 则先查 `stems`（特殊词干编码），再查 `words`（单字编码表，过滤掉权重低于总量 5% 的冷门编码，见 [entry_collector.cc:223-245](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/entry_collector.cc#L223-L245)）。

完整闭环：`*.dict.yaml` 无码词 → `encode_queue` → `EncodePhrase` → DFS 逐字/逐词 → `TranslateWord` 取单字码 → `Encode` 套公式（形码）/直接拼（音码）→ `CreateEntry` 入库 → 进入 `entries` → 由 `DictCompiler` 写进 `.table.bin`（见 [u8-l4](u8-l4-dict-compiler.md)）。

#### 4.4.4 代码实践

**目标**：追踪一个双字词从「无码」到「被编码入库」的完整调用链，理解它如何最终交给 `PhraseCollector::CreateEntry`。

**操作步骤**：

1. 想象 `cangjie5.dict.yaml` 里有一行只有文字、没有 code 列的双字词（词组库常见）。
2. 在源码里按顺序定位：Pass 1 入队 [entry_collector.cc:119-121](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/entry_collector.cc#L119-L121) → Pass 2 出队调 `EncodePhrase` [L138](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/entry_collector.cc#L138) → `TableEncoder::EncodePhrase` [encoder.cc:236-245](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/encoder.cc#L236-L245) → DFS 逐字 `DfsEncode` → 末尾 `Encode`+`CreateEntry` [L256-261](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/encoder.cc#L256-L261)。
3. 在 `EncodePhrase` 处注意长度守卫：`if ((int)phrase_length > max_phrase_length_) return false;`（[L239-240](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/encoder.cc#L239-L240)），超长词直接放弃。

**需要观察的现象**：`max_phrase_length_` 由所有规则的 `max_word_length` 取上界得到（上限 `kMaxPhraseLength=32`），所以仓颉配置里最长规则 `[4,10]` 会让 `max_phrase_length_=10`，11 字及以上的词不会被编码。

**预期结果**：能画出「dict.yaml 无码行 → encode_queue → EncodePhrase → DfsEncode(逐字) → TranslateWord → Encode(公式) → CreateEntry → entries」的完整时序。

**待本地验证**：可在 `EntryCollector::Finish` 与 `TableEncoder::DfsEncode` 各加一行日志（如打印 `phrase` 与最终 `encoded`），重新构建词典（`rime_api_console` 首次部署会触发）观察输出。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `ScriptEncoder` 不需要 `LoadSettings` 读公式？
**答案**：音码的词组编码就是把各组成词的音节按顺序拼接，没有「取第几个字的第几个码」的规则，因此没有公式可配。`ScriptEncoder` 没有重写 `Encoder::LoadSettings`，沿用基类空实现。

**练习 2**：`kEncoderDfsLimit = 32` 限制的是什么？为什么需要它？
**答案**：限制一次 `EncodePhrase` 调用中 DFS 探索的叶子（成功到达末尾的路径）总数。当短语里有很多多音字/多编码字时，组合数会指数膨胀；这个上限让编码器在「穷举」与「性能」之间取平衡，超过就提前返回已找到的结果。

**练习 3**：`TranslateWord` 查 `words` 表时，为什么用 `kMinimalWeight = 0.05` 过滤？
**答案**：只保留权重不低于该字总权重 5% 的编码，剔除极冷门的多音字编码，避免给词组生成出冷僻、几乎不会被用到的编码，控制索引膨胀（见 [entry_collector.cc:236-241](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/entry_collector.cc#L236-L241)）。

## 5. 综合实践

**任务**：为一套「自造双字形码」设计 `encoder` 配置，并全程手动追踪一个双字词的编码生成。

1. **写配置**：仿照 [cangjie5.dict.yaml:30-41](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/cangjie5.dict.yaml#L30-L41)，为一个假想形码方案写 `encoder` 段：要求双字词取「第 1 字首尾 + 第 2 字首尾」共 4 码。写出对应公式（答案：`AaAzBaBz`），并配 `length_equal: 2`。
2. **解析公式**：手算你的公式解析成 `CodeCoords` 序列，对照 `ParseFormula` 的分段函数验证每个 `char_index`/`code_index`。
3. **追踪取码**：设双字词两字的单字编码为 `code[0]="wxyz"`、`code[1]="mnop"`，用 `Encode` 的逻辑（负索引归位、同字推进、尾锚）推出最终编码（答案：`Aa`='w'、`Az`='z'、`Ba`='m'、`Bz`='p' → `"wzmp"`）。
4. **追踪入库**：写出这条词从 `encode_queue` 到 `CreateEntry("双字词", "wzmp", weight)` 经过的函数调用序列，指出哪一步「交给 PhraseCollector」。
5. **验证**：把上述公式与 `RawCode "wxyz mnop"` 写进一个仿 `RimeEncoderTest.Encode` 的临时用例，编译运行，确认 `result == "wzmp"`。

通过这五步，你把「公式 DSL → 解析 → 取码 → DFS → 回调入库」整条链路亲手走了一遍。

## 6. 本讲小结

- Encoder 体系是三件套：`RawCode`（编码数据形态，字符串数组）、`PhraseCollector`（回调接口，`CreateEntry` 入库 / `TranslateWord` 取单字码）、`Encoder`（编码算法基类）。编码器只「算」，通过回调把结果「推回」收集器。
- 形码的公式 DSL 用一对字母表达「第几个字的第几个码」：大写 `A`~`T` 正向 / `U`~`Z` 反向定位字，小写 `a`~`t` 正向 / `u`~`z` 反向定位码；`ParseFormula` 用分段函数把字母译成 `CodeCoords`，负索引运行期加字数归位。
- `TableEncoder::Encode` 按第一条匹配字长的规则，逐个 `CodeCoords` 取码拼接，带「同字推进防重复、越界跳过保健壮」两道守卫；`CalculateCodeIndex` 配合 `tail_anchor` 让仓颉复合字的「尾码」停在主码段末尾。
- 形码用 `TableEncoder.DfsEncode` **逐字**切分 + 套公式；音码用 `ScriptEncoder.DfsEncode` **逐词**（多种词长）切分 + 直接拼接音节，到达末尾不套公式。两者都受 `kEncoderDfsLimit=32` 约束。
- `EntryCollector::Configure` 以 `encoder/rules` 是否存在二选一选编码器（`use_rule_based_encoder()`），`Finish` 的 Pass 2 把 dict.yaml 里无码词送进 `EncodePhrase` 自动补码，形成「无码词 → DFS → 公式/拼接 → CreateEntry → entries → .table.bin」的闭环。

## 7. 下一步学习建议

- 回到 [u8-l4 DictCompiler 构建流程](u8-l4-dict-compiler.md)，把本讲的「Pass 2 编码」嵌回三遍收集的全景，理解 `entries` 如何被 `Vocabulary` 组织并写进 `.table.bin`。
- 继续读 [src/rime/algo/encoder.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/encoder.cc) 中 `Encode` 的其余守卫（L162-186 的反向索引防回退逻辑），它们处理 `AaBaYaZaZz` 这类混合正反向的复杂公式。
- 阅读 [u8-l5 Dictionary 查询主链路](u8-l5-dictionary-lookup.md)，看本讲产出的 `.table.bin` 词条在运行期如何被 `Dictionary::Lookup` 查出来。
- 想动手扩展可参考 [sample](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/sample) 插件骨架，尝试实现一个自定义 `Encoder` 子类（如基于笔画数的编码器），并用 `EntryCollector::set_collector` 或继承 `PhraseCollector` 接入。
