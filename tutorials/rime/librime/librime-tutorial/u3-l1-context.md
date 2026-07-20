# Context：输入状态容器

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 `Context` 在 librime 运行时中扮演的「中央状态容器」角色，以及它与 `Engine`、各组件（Processor/Segmentor/Translator/Filter）的关系。
- 掌握 `input`（原始输入串）、`caret_pos`（光标位置）、`composition`（候选组织）三组核心字段的读写入口与触发时机。
- 理解 `CommitHistory` 如何以环形缓冲记录最近 20 条提交记录，以及它在「学习用户输入」中的作用。
- 区分 `options` / `properties` 的**会话级**与**方案级（schema-local）**两层作用域，看懂以下划线 `_` 开头的名字含义。
- 列出 `Context` 暴露的全部 8 类 `Notifier` 信号，并说明每种信号在何时被触发、谁在监听。

本讲是单元 u3（输入状态与候选生成）的第一篇，承接 u2-l4 的 Engine 骨架：上讲我们看到 `Engine` 独占持有一个 `Context`，并把方案的 `engine/{processors,segmentors,translators,filters}` 四张清单装配成流水线。本讲就来拆开这个被流水线反复读写的「工作台」。

## 2. 前置知识

在进入源码前，先用三个生活化的比喻建立直觉：

1. **「工作台」比喻**：把 `Context` 想象成一块共享的工作台。Processor 是「把零件放上台面」的人（写入 `input`），Segmentor/Translator/Filter 是「在台面上加工」的人（读写 `composition`），前端是「来台面取货」的人（读 `preedit` 与候选）。工作台本身不主动做事，只提供**状态**和**通知**。
2. **「观察者模式（Observer）」**：`Notifier` 本质是 Boost.Signals2 的 `signal`（在 `common.h` 里别名为 `signal`，见 u1-l3）。组件不直接互调，而是「订阅信号」——当 `Context` 状态变化，它就向所有订阅者广播。这是 librime 解耦的核心手段。
3. **「拉模型」与「推模型」」**：这一点在 u2-l2 已建立——提交文本走「拉」（前端主动 `get_commit`），状态通知走「推」（引擎 `message_sink_` 推给前端）。`Context` 的 Notifier 是引擎**内部**的推通道，最终再由 Engine 转译成对外的 `message_sink_`。

此外需要 recall 的术语（来自前几讲）：

- `Engine` 持有 `schema_`（方案）与 `context_`（本讲主角），见 u2-l4。
- `an<T>` / `of<T>` / `the<T>` 是 `common.h` 里的智能指针别名。
- `Segment::Status` 有 `kVoid / kGuess / kSelected / kConfirmed` 四态，见 segmentation.h（下一讲 u3-l2 详述）。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [src/rime/context.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/context.h) | `Context` 类声明 | 全部公共接口、私有字段、8 类 Notifier 别名 |
| [src/rime/context.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/context.cc) | `Context` 实现 | `Commit`/`PushInput`/`Select`/`set_option` 等方法如何触发 Notifier |
| [src/rime/commit_history.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/commit_history.h) | `CommitHistory` / `CommitRecord` | 环形缓冲、`kMaxRecords = 20` |
| [src/rime/commit_history.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/commit_history.cc) | `CommitHistory` 实现 | 三种 `Push` 重载、`repr` |
| [src/rime/engine.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc) | `ConcreteEngine` | 如何把 5 类 Notifier 接到引擎回调（关键的「订阅者」样例） |

`Composition` 与 `Segmentation` 只在本讲作为「字段类型」出现，它们的内部结构留待 u3-l2 / u3-l3 展开。

---

## 4. 核心概念与源码讲解

### 4.1 Context 总览：输入状态的中央容器

#### 4.1.1 概念说明

`Context` 是「**当前这一次输入会话的瞬时状态**」的总集合体。注意三个定语：

- **当前**：它只描述「眼下的输入」，不存历史方案、不存用户词典（那些在 `user_db`）。
- **输入会话**：一个 `Session` 持有一个 `Engine`，一个 `Engine` 独占一个 `Context`（见 u2-l4 `Engine::Engine()` 里 `context_(new Context)`）。
- **瞬时状态**：提交（commit）之后状态会被 `Clear()` 重置，进入下一轮输入。

它解决的问题是：流水线上四类组件（Processor/Segmentor/Translator/Filter）需要一个**共享的、可读写的、能广播变化**的地方。`Context` 就是这块地方。

#### 4.1.2 核心流程

`Context` 的生命周期与一次「输入 → 提交」回合同步：

```
回合开始（输入第一个键）
   └─ Processor 调 PushInput/set_input 写入 input_
        └─ 触发 update_notifier_  ──► Engine::OnContextUpdate ──► Compose
              └─ Segmentor/Translator/Filter 填充 composition_
   ...用户选择候选 / 继续敲键...
回合结束
   └─ Engine 调 ctx->Commit()
        └─ 触发 commit_notifier_ ──► Engine::OnCommit ──► sink_(text) 提交给前端
        └─ Clear() 清空 input_/composition_，触发 update_notifier_
```

关键在于：**几乎每一个改变状态的方法，都会在最后触发一次 Notifier**。组件之间靠这些信号协作，而不是直接互调。

#### 4.1.3 源码精读

类的全貌在 [context.h:L19-L116](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/context.h#L19-L116)，其中 `RIME_DLL` 让该类能随动态库导出给插件使用。私有字段集中体现了「它装了什么」：

```cpp
// context.h:L101-L106
string input_;
size_t caret_pos_ = 0;
Composition composition_;
CommitHistory commit_history_;
map<string, bool> options_;
map<string, string> properties_;
```

这 6 个字段就是 `Context` 的全部状态：原始输入串、光标位置、候选组织、提交历史、布尔开关、字符串属性。紧接着是 8 个 Notifier 信号（见 4.5）。

`Commit()` 是回合结束的入口，它体现了「先通知、再清空」的顺序：

```cpp
// context.cc:L18-L26
bool Context::Commit() {
  if (!IsComposing())
    return false;
  commit_notifier_(this);  // 先通知：让 Engine 取走提交文本
  Clear();                 // 再清空，开始下一回合
  return true;
}
```

`IsComposing()` 判定「是否处于输入态」——只要 `input_` 非空或 `composition_` 非空就算在输入中：

```cpp
// context.cc:L48-L50
bool Context::IsComposing() const {
  return !input_.empty() || !composition_.empty();
}
```

> 注意：`commit_notifier_` 的回调（`Engine::OnCommit`）是在 `Clear()` **之前**执行的，因此它还能读到本次的 `composition_` 与 `input_`。这是「先通知、后清空」顺序的意义。

#### 4.1.4 代码实践

1. **目标**：确认「谁拥有 Context、Context 装了什么」。
2. **步骤**：打开 [engine.cc:L64-L69](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L64-L69)，看 `Engine` 构造里 `context_(new Context)` 与析构里 `context_.reset()`；再对照 [context.h:L101-L106](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/context.h#L101-L106) 的 6 个私有字段。
3. **观察**：`Context` 是默认构造（`Context() = default;`，[context.h:L28](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/context.h#L28)），所有字段都有类内初值，因此 `new Context` 之后无需额外初始化即可使用。
4. **预期结果**：能画出 `Session ──owns──► Engine ──owns──► Context` 的持有链，并说出 Context 的 6 个字段名。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Commit()` 里要先 `commit_notifier_(this)` 再 `Clear()`，顺序能反过来吗？

**答案**：不能。`commit_notifier_` 的订阅者（`Engine::OnCommit`）需要从 `composition_` 与 `input_` 里提取本次提交文本（[engine.cc:L251-L257](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L251-L257)）。若先 `Clear()`，这些数据已被清空，提交文本就丢失了。

**练习 2**：`IsComposing()` 在什么情况下 `input_` 为空但 `composition_` 非空？

**答案**：候选已选定但尚未提交、或正处于「分段转换」中途时可能发生（例如 `OnSelect` 里 `Forward()` 出空段，见 [engine.cc:L259-L282](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L259-L282)）。只要二者之一非空就视为「仍在输入态」。

---

### 4.2 input 与 caret_pos：原始输入的读写

#### 4.2.1 概念说明

`input_` 是**用户敲出的原始字符序列**（如拼音输入的 `"nihao"`），`caret_pos_` 是光标在这个串里的字节偏移。这两者是流水线最上游的输入。

注意 `input_` 是「原始字符」，**不是音节、不是候选**。它可能包含字母、数字、甚至标点；切分音节是 Segmentor 的职责（下讲 u3-l2 / u6-l3）。光标之所以重要，是因为 librime 支持「**编辑插入**」——用户可以把光标移到串中间，在中间插入或删除字符，再重新切分。

#### 4.2.2 核心流程

`Context` 提供两类写入入口：

- **增量编辑**：`PushInput`（在光标处追加）、`PopInput`（光标左侧删除）、`DeleteInput`（光标右侧删除）——这些会**移动光标**并触发 `update_notifier_`。
- **整体替换**：`set_input(str)`——把整个串替换掉，并把光标置于末尾。

读取入口则是内联的 `input()` / `caret_pos()` 访问器。光标移动用 `set_caret_pos`，它会把越界值夹到串尾。

```
PushInput('a')  ──►  input_ 插入 'a'，caret_pos_++，触发 update_notifier_
PopInput(1)     ──►  caret_pos_--，input_.erase(...)，触发 update_notifier_
set_input("xyz") ──► input_ = "xyz"，caret_pos_ = 3，触发 update_notifier_
```

每一次写入都会触发 `update_notifier_`，这正是 Engine 得以感知「输入变了、该重新 Compose 了」的机制。

#### 4.2.3 源码精读

两个 `PushInput` 重载都区分「光标在末尾」与「光标在中间」两种情形，后者要走 `insert`：

```cpp
// context.cc:L65-L75
bool Context::PushInput(char ch) {
  if (caret_pos_ >= input_.length()) {
    input_.push_back(ch);
    caret_pos_ = input_.length();
  } else {
    input_.insert(caret_pos_, 1, ch);
    ++caret_pos_;
  }
  update_notifier_(this);
  return true;
}
```

`PopInput` 与 `DeleteInput` 在越界时返回 `false` 而非抛异常，这是 librime 的容错风格：

```cpp
// context.cc:L89-L96
bool Context::PopInput(size_t len) {
  if (caret_pos_ < len)
    return false;
  caret_pos_ -= len;
  input_.erase(caret_pos_, len);
  update_notifier_(this);
  return true;
}
```

`set_input` 会把光标强制移到末尾——它假设「整体替换后从头输入」：

```cpp
// context.cc:L280-L284
void Context::set_input(const string& value) {
  input_ = value;
  caret_pos_ = input_.length();
  update_notifier_(this);
}
```

而 `set_caret_pos` 单独移动光标（不改变 `input_`），并对越界做夹断：

```cpp
// context.cc:L268-L274
void Context::set_caret_pos(size_t caret_pos) {
  if (caret_pos > input_.length())
    caret_pos_ = input_.length();
  else
    caret_pos_ = caret_pos;
  update_notifier_(this);
}
```

> 实践中谁在调用这些？最典型的就是 `speller` Processor——它捕获字母键，调用 `ctx->PushInput(ch)` 把拼音追加进 `input_`（详见 u6-l2）。

#### 4.2.4 代码实践

1. **目标**：理解光标在中间时的插入/删除行为，以及「每次写入都广播」。
2. **步骤**：阅读 [context.cc:L65-L104](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/context.cc#L65-L104) 的 `PushInput`/`PopInput`/`DeleteInput` 三个方法。
3. **观察**：假设 `input_="ab"`, `caret_pos_=1`（光标在 a 与 b 之间）。
   - 调 `PushInput('X')` 走哪个分支？结果 `input_` 应为 `"aXb"`，`caret_pos_=2`。
   - 调 `PopInput(1)` 后 `input_="Xb"`，`caret_pos_=1`。
   - 调 `DeleteInput(1)`（光标右侧）后 `input_="ab"`，`caret_pos_=1`。
4. **预期结果**：能口头推演上述三步，并指出每个方法最后一行都是 `update_notifier_(this);`。

#### 4.2.5 小练习与答案

**练习 1**：`set_input` 和连续调用 `PushInput` 拼出同样的串，行为完全等价吗？

**答案**：不等价。`set_input` 是整体替换，会**把光标放到串尾**；而 `PushInput` 从空串开始追加时虽然结果串相同、光标也都在末尾，但中途会触发**多次** `update_notifier_`（每按一次键一次），每次都可能引发一次 `Compose`。`set_input` 只触发一次。

**练习 2**：`PopInput(len)` 的 `len` 大于当前 `caret_pos_` 时会怎样？

**答案**：直接返回 `false`，不做任何修改、也不触发 `update_notifier_`（见 [context.cc:L90-L91](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/context.cc#L90-L91)）。这是一种「无操作即不通知」的约定。

---

### 4.3 Composition 与候选编辑操作

#### 4.3.1 概念说明

`composition_` 是 `Composition` 类型（继承自 `Segmentation`，即一组 `Segment`）。它装的是「输入串被切分后、挂上候选的结果」。本讲只把它当作「候选组织」这个黑盒字段来用，下一讲 u3-l2 / u3-l3 才拆开它的内部。

围绕 `composition_`，`Context` 提供了一组**候选编辑操作**：选择（`Select`）、高亮（`Highlight`）、删除候选（`DeleteCandidate`）、确认当前选择（`ConfirmCurrentSelection`），以及一组「回退/重开」操作（`ReopenPreviousSegment`、`ClearPreviousSegment`、`ReopenPreviousSelection` 等），用于支持用户「上一步选错了，我要回去改」的交互。

#### 4.3.2 核心流程

候选编辑的核心是「修改最后一个 Segment 的 `status` 与 `selected_index`，再触发对应 Notifier」：

```
Select(index)            ──► seg.status = kSelected, seg.selected_index = index ──► select_notifier_
Highlight(index)         ──► seg.selected_index = new_index（若变化）         ──► update_notifier_
DeleteCandidate(index)   ──► seg.selected_index = index                       ──► delete_notifier_
ConfirmCurrentSelection()──► seg.status = kSelected                            ──► select_notifier_
```

`Select` 与 `ConfirmCurrentSelection` 的区别值得注意：

- `Select(i)` 要求 `i` 处**必须存在候选**，否则返回 `false`。
- `ConfirmCurrentSelection()` 是「确认当前高亮项」，即使没有候选也会把状态推进（用于「确认原始输入」的回退路径）。

#### 4.3.3 源码精读

`Select` 是最典型的「改状态 + 通知」模式：

```cpp
// context.cc:L118-L130
bool Context::Select(size_t index) {
  if (composition_.empty())
    return false;
  Segment& seg(composition_.back());
  if (auto cand = seg.GetCandidateAt(index)) {
    seg.selected_index = index;
    seg.status = Segment::kSelected;
    DLOG(INFO) << "Selected: '" << cand->text() << "', index = " << index;
    select_notifier_(this);
    return true;
  }
  return false;
}
```

`Highlight` 的巧妙之处：它会在通知前比较新旧索引，**未变化就不通知**，避免无谓的重算：

```cpp
// context.cc:L132-L149（节选）
size_t candidate_count = seg.menu->Prepare(index + 1);
size_t new_index =
    candidate_count > 0 ? (std::min)(candidate_count - 1, index) : 0;
size_t previous_index = seg.selected_index;
if (previous_index == new_index) {
  DLOG(INFO) << "selection has not changed, ...";
  return false;        // 没变化：不触发通知
}
seg.selected_index = new_index;
update_notifier_(this);
```

`ConfirmCurrentSelection` 展示了「无候选也能确认」的回退分支（用于确认原始输入，例如形码里敲了词典没有的码）：

```cpp
// context.cc:L168-L185（节选）
seg.status = Segment::kSelected;
if (auto cand = seg.GetSelectedCandidate()) {
  DLOG(INFO) << "Confirmed: '" << cand->text() << "', ...";
} else {
  if (seg.end == seg.start) {
    return false;      // 空段：交给 fluid_editor 去整句确认
  }
  // confirm raw input（无候选则确认原始输入）
}
select_notifier_(this);
```

「回退」类操作里，`ClearNonConfirmedComposition` 会把末尾所有「尚未确认」的段弹出，再 `Forward()` 推进边界——它在选项变化时被用来重算候选（见 4.4）：

```cpp
// context.cc:L246-L258（节选）
while (!composition_.empty() &&
       composition_.back().status < Segment::kSelected) {
  composition_.pop_back();
  reverted = true;
}
if (reverted) {
  composition_.Forward();
}
```

#### 4.3.4 代码实践

1. **目标**：分清 `Select`、`Highlight`、`ConfirmCurrentSelection`、`DeleteCandidate` 各自触发哪类 Notifier。
2. **步骤**：阅读 [context.cc:L118-L166](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/context.cc#L118-L166)。
3. **观察**：填一张表——

   | 方法 | 改变的字段 | 触发的 Notifier |
   | --- | --- | --- |
   | `Select(i)` | `status=kSelected`, `selected_index=i` | `select_notifier_` |
   | `Highlight(i)` | `selected_index` | `update_notifier_`（变化时） |
   | `DeleteCandidate(i)` | `selected_index=i` | `delete_notifier_` |
   | `ConfirmCurrentSelection()` | `status=kSelected` | `select_notifier_` |

4. **预期结果**：注意 `DeleteCandidate` 的注释 `CAVEAT: this doesn't mean anything is deleted for sure`（[context.cc:L158](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/context.cc#L158)）——它只是「请求删除」，真正的删除由订阅 `delete_notifier_` 的组件（如用户词典）执行。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Highlight` 在「索引未变化」时要返回 `false` 且不触发通知？

**答案**：高亮变化会引发前端重绘候选列表。如果按住方向键持续触发、但索引已到边界（`new_index` 被 `min` 夹住不变），不返回 `false` 就会反复广播 `update_notifier_`、反复重算 `Compose`，造成无谓开销。

**练习 2**：`Select` 与 `ConfirmCurrentSelection` 都触发 `select_notifier_`，它们的语义差别在哪？

**答案**：`Select(i)` **按索引选**，要求该索引必须有候选；`ConfirmCurrentSelection()` **确认当前高亮**，且在没有候选时仍可推进状态（走「确认原始输入」分支）。前者是「选第 i 个」，后者是「敲回车确认现在这个」。

---

### 4.4 CommitHistory：提交历史

#### 4.4.1 概念说明

`CommitHistory` 记录「**最近提交了什么**」——不是原始按键，而是经过翻译、被用户确认的文本片段。它的用途是给翻译器提供上下文：例如用户刚提交了「你」，下一次输入「hao」时，翻译器可以参考「你」来给「你好」更高权重。

它本质是一个**有界双端队列（容量 20）**：继承自 `list<CommitRecord>`，每次 `Push` 后若超容就 `pop_front()` 丢弃最旧记录。

#### 4.4.2 核心流程

`CommitRecord` 只有 `type`（类型标签）与 `text`（文本）两个字段。`CommitHistory` 提供三个 `Push` 重载，对应三种来源：

```
Push(CommitRecord)              ── 通用：直接压入，超容丢最旧
Push(const KeyEvent&)           ── 按键：仅记录可打印 ASCII，遇 BackSpace/Return 清空
Push(const Composition&, input) ── 提交：把整段 composition 翻译成记录序列
```

容量上界由常量定义：

\[ \text{size} \le kMaxRecords = 20 \]

每来一条新记录，若 `size > 20` 就从队首丢一条，维持窗口长度恒为 20。

#### 4.4.3 源码精读

数据结构定义在 [commit_history.h:L14-L33](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/commit_history.h#L14-L33)：

```cpp
// commit_history.h:L14-L20
struct CommitRecord {
  string type;
  string text;
  CommitRecord(const string& a_type, const string& a_text)
      : type(a_type), text(a_text) {}
  CommitRecord(int keycode) : type("thru"), text(1, keycode) {}
};
```

注意第二个构造函数：把单个 ASCII 码包装成 `type="thru"` 的记录（"through"，意为「直接透传的按键」）。

```cpp
// commit_history.h:L25-L33
class CommitHistory : public list<CommitRecord> {
 public:
  static const size_t kMaxRecords = 20;
  void Push(const CommitRecord& record);
  void Push(const KeyEvent& key_event);
  void Push(const Composition& composition, const string& input);
  string repr() const;
  string latest_text() const { return empty() ? string() : back().text; }
};
```

通用 `Push` 维护窗口不变量：

```cpp
// commit_history.cc:L14-L18
void CommitHistory::Push(const CommitRecord& record) {
  push_back(record);
  if (!empty() && size() > kMaxRecords)
    pop_front();
}
```

按键重载体现了「语义化过滤」：只有无修饰的可打印 ASCII 才记录，遇到退格/回车则**整体清空**（因为这些键意味着用户在重新编辑或结束输入，历史不再连续）：

```cpp
// commit_history.cc:L20-L30
void CommitHistory::Push(const KeyEvent& key_event) {
  if (key_event.modifier() == 0) {
    if (key_event.keycode() == XK_BackSpace ||
        key_event.keycode() == XK_Return) {
      clear();
    } else if (key_event.keycode() >= 0x20 && key_event.keycode() <= 0x7e) {
      // printable ascii character
      Push(CommitRecord(key_event.keycode()));
    }
  }
}
```

提交重载最复杂：它遍历 `composition` 的每个 Segment，把已选候选的 `type()/text()` 串成记录，相邻同类型的候选还会**合并文本**；没有翻译的段则记为 `{"raw", ...}`：

```cpp
// commit_history.cc:L32-L59（节选）
for (const Segment& seg : composition) {
  if (auto cand = seg.GetSelectedCandidate()) {
    if (last && last->type == cand->type()) {
      last->text += cand->text();   // 同类型相邻：合并
    } else {
      Push({cand->type(), cand->text()});
      last = &back();
    }
    if (seg.status >= Segment::kConfirmed) {
      last = NULL;                  // 已确认段：终止当前记录
    }
    end = cand->end();
  } else {
    Push({"raw", input.substr(seg.start, seg.end - seg.start)});
    end = seg.end;
  }
}
```

> 这个 `Push(composition, input)` 由 `Engine::OnCommit` 调用（[engine.cc:L251-L252](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L251-L252)），是「真正提交」时记录历史的入口。而按键重载则由 `ProcessKey` 在按键未被任何 Processor 接管时调用（[engine.cc:L110](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L110)），用来记录「透传的原始按键」。

#### 4.4.4 代码实践

1. **目标**：理解 `CommitHistory` 的「同类型合并」与「窗口容量」。
2. **步骤**：阅读 [commit_history.cc:L14-L18](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/commit_history.cc#L14-L18) 与 [commit_history.cc:L32-L59](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/commit_history.cc#L32-L59)。
3. **观察**：假设一次提交的 composition 含三个候选段，类型依次为 `("table", "你")`、`("table", "好")`、`("punct", "！")`。
   - 前两个同为 `table`，会被合并成一条 `[table]你好`。
   - 第三个 `punct` 不同，另起一条 `[punct]！`。
   - 最终 `repr()` 返回 `"[table]你好[punct]！"`。
4. **预期结果**：能解释为什么 `repr()` 把每条记录格式化成 `[type]text`（见 [commit_history.cc:L61-L67](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/commit_history.cc#L61-L67)），并知道窗口最多保留 20 条。

#### 4.4.5 小练习与答案

**练习 1**：用户按了 `BackSpace`，`CommitHistory` 会发生什么？

**答案**：在 `ProcessKey` 里若该键未被 Processor 接管，会调 `commit_history().Push(key_event)`；该重载检测到 `XK_BackSpace` 且无修饰键，会 `clear()` 整个历史（[commit_history.cc:L22-L24](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/commit_history.cc#L22-L24)）。语义是「用户开始往回删，之前的输入上下文不再连续」。

**练习 2**：`CommitHistory` 为什么继承自 `list<CommitRecord>` 而不是封装一个 list？

**答案**：为了直接复用 `list` 的全部接口（`back()`/`empty()`/迭代器等），代码更简洁。`latest_text()` 就是直接调 `list::back()`（[commit_history.h:L32](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/commit_history.h#L32)）。这是 librime 里常见的「is-a 复用标准容器」风格（`Composition : Segmentation : vector<Segment>` 也是如此）。

---

### 4.5 options / properties 与 Notifier 信号体系

#### 4.5.1 概念说明

`Context` 同时是「开关仓库」和「事件总线」。

- **options**（`map<string,bool>`）：布尔开关，如 `ascii_mode`（中/英文模式）、`full_shape`（全/半角）。组件通过 `get_option/set_option` 读写。
- **properties**（`map<string,string>`）：字符串属性，用于比开关更复杂的带值状态（如当前方案名）。
- **Notifier**（8 类信号）：当状态变化时广播给订阅者。

**两层作用域**是本模块的关键约定（见 [context.h:L81-L83](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/context.h#L81-L83) 的注释）：

- 名字**以下划线 `_` 开头**的 option/property 是**方案级（schema-local）**——换方案时会被清掉。
- 其他名字是**会话级（session-scoped）**——在整个会话内持续有效，换方案也不丢。

#### 4.5.2 核心流程

`set_option` / `set_property` 的实现极其简单——写入 map 后立刻触发对应 Notifier：

```
set_option(name, value)     ── options_[name] = value ──► option_update_notifier_(this, name)
set_property(name, value)   ── properties_[name] = value ──► property_update_notifier_(this, name)
get_option(name)            ── 查 map，找不到默认 false
get_property(name)          ── 查 map，找不到默认空串
ClearTransientOptions()     ── 删除所有以 '_' 开头的项（换方案时调用）
```

`Context` 暴露的全部 8 类 Notifier：

| Notifier | 类型别名 | 触发时机 |
| --- | --- | --- |
| `commit_notifier_` | `Notifier` | `Commit()` 时，清空状态前 |
| `select_notifier_` | `Notifier` | `Select` / `ConfirmCurrentSelection` 选中候选时 |
| `update_notifier_` | `Notifier` | 几乎所有状态变化（输入、光标、高亮、重开段等） |
| `delete_notifier_` | `Notifier` | `DeleteCandidate` 请求删除候选时 |
| `abort_notifier_` | `Notifier` | `AbortComposition()` 时（清空并通知放弃） |
| `option_update_notifier_` | `OptionUpdateNotifier` | `set_option` 时 |
| `property_update_notifier_` | `PropertyUpdateNotifier` | `set_property` 时 |
| `unhandled_key_notifier_` | `KeyEventNotifier` | 按键未被任何 Processor 接管时（由 Engine 触发） |

#### 4.5.3 源码精读

Notifier 的类型别名定义在类开头，参数列表体现了各自携带的信息量：

```cpp
// context.h:L21-L26
using Notifier = signal<void(Context* ctx)>;
using OptionUpdateNotifier = signal<void(Context* ctx, const string& option)>;
using PropertyUpdateNotifier =
    signal<void(Context* ctx, const string& property)>;
using KeyEventNotifier =
    signal<void(Context* ctx, const KeyEvent& key_event)>;
```

`set_option` / `get_option` 一眼可见其「写即通知 / 读则兜底」的设计：

```cpp
// context.cc:L286-L298
void Context::set_option(const string& name, bool value) {
  options_[name] = value;
  DLOG(INFO) << "Context::set_option " << name << " = " << value;
  option_update_notifier_(this, name);
}

bool Context::get_option(const string& name) const {
  auto it = options_.find(name);
  if (it != options_.end())
    return it->second;
  else
    return false;        // 未设置的开关默认 false
}
```

`ClearTransientOptions` 用 `lower_bound("_")` 定位到所有以下划线开头的键并批量删除——这就是「方案级」作用域的实现机制：

```cpp
// context.cc:L313-L325（节选）
auto opt = options_.lower_bound("_");
while (opt != options_.end() && !opt->first.empty() && opt->first[0] == '_') {
  options_.erase(opt++);
}
```

它由 `Engine::ApplySchema`（换方案）调用，确保换方案后不会残留旧方案的私有开关：

```cpp
// engine.cc:L284-L289（节选）
void ConcreteEngine::ApplySchema(Schema* schema) {
  ...
  context_->Clear();
  context_->ClearTransientOptions();
  ...
}
```

**谁在订阅这些 Notifier？** `ConcreteEngine` 构造时连接了其中 5 个，这是理解「Notifier → Engine 行为」的关键样例：

```cpp
// engine.cc:L74-L85
context_->commit_notifier().connect([this](Context* ctx) { OnCommit(ctx); });
context_->select_notifier().connect([this](Context* ctx) { OnSelect(ctx); });
context_->update_notifier().connect(
    [this](Context* ctx) { OnContextUpdate(ctx); });
context_->option_update_notifier().connect(
    [this](Context* ctx, const string& option) { OnOptionUpdate(ctx, option); });
context_->property_update_notifier().connect(
    [this](Context* ctx, const string& property) { OnPropertyUpdate(ctx, property); });
```

读这段要建立一条因果链：

- `update_notifier_` → `OnContextUpdate` → `Compose`（[engine.cc:L124-L128](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L124-L128)）：**所以每次 `PushInput`/`set_input`/`Highlight` 都会引发一次重新切分与翻译**。
- `option_update_notifier_` → `OnOptionUpdate`：若正在输入，会 `RefreshNonConfirmedComposition()` 重算候选，并把开关变化以 `message_sink_("option", ...)` 推给前端（[engine.cc:L130-L142](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L130-L142)）。
- `commit_notifier_` → `OnCommit` → `sink_(text)`：提交文本最终经 `sink_` 流向 `Session::OnCommit`（见 u2-l2）。

`unhandled_key_notifier_` 与前 5 个不同——它**不是**在某次状态变化时触发，而是由 `ConcreteEngine::ProcessKey` 在按键穿过整条 Processor 链仍未被接受后**主动调用**：

```cpp
// engine.cc:L120
context_->unhandled_key_notifier()(context_.get(), key_event);
```

> 真实组件里 options 的使用随处可见，可印证「会话级 vs 方案级」约定：
> - 会话级（无下划线）：`ascii_mode`（[ascii_segmentor.cc:L20](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/ascii_segmentor.cc#L20)）、`full_shape`（[punctuator.cc:L24](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/punctuator.cc#L24)）、`extended_charset`（[charset_filter.cc:L104](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/charset_filter.cc#L104)）、`ascii_punct`（[punctuator.cc:L102](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/punctuator.cc#L102)）。
> - 方案级（下划线开头）：`_auto_commit`（[speller.cc:L208](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/speller.cc#L208)）、`_vertical`（[navigator.cc:L95](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/navigator.cc#L95)）、`_chord_typing`（[chord_composer.cc:L47](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/chord_composer.cc#L47)）、`_fold_options`（[switch_translator.cc:L238](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/switch_translator.cc#L238)）。

#### 4.5.4 代码实践（本讲核心实践任务）

1. **目标**：列出 `Context` 暴露的全部 Notifier，并说明每种信号「在何时被触发」「被谁订阅」「导致什么后续行为」。
2. **操作步骤**：
   - 打开 [context.h:L85-L96](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/context.h#L85-L96)，确认 8 个 `*_notifier()` 访问器。
   - 在 [context.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/context.cc) 中用搜索定位每个 `*_notifier_(this)` 的调用点，确认触发方法。
   - 在 [engine.cc:L74-L85](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L74-L85) 确认 Engine 订阅了哪 5 个；`unhandled_key_notifier_` 的触发点在 [engine.cc:L120](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L120)。
3. **完成下表**（待本地验证你填写的「订阅者行为」列是否与源码一致）：

   | Notifier | 触发方法（context.cc） | Engine 订阅回调 | 后续行为 |
   | --- | --- | --- | --- |
   | `commit_notifier_` | `Commit` (L22) | `OnCommit` | 取提交文本 → `sink_` |
   | `select_notifier_` | `Select`/`ConfirmCurrentSelection` | `OnSelect` | `seg.Close()`，可能继续 Compose 或 Commit |
   | `update_notifier_` | `PushInput`/`set_input`/`set_caret_pos`/`Highlight`/`Clear` 等 | `OnContextUpdate` | `Compose`（重算切分与翻译） |
   | `delete_notifier_` | `DeleteCandidate` | （Engine 未订阅；由用户词典等组件订阅） | 删除词条 |
   | `abort_notifier_` | `AbortComposition` | （Engine 未订阅） | 通知放弃本次输入 |
   | `option_update_notifier_` | `set_option` | `OnOptionUpdate` | 重算候选 + `message_sink_("option",…)` |
   | `property_update_notifier_` | `set_property` | `OnPropertyUpdate` | `message_sink_("property",…)` |
   | `unhandled_key_notifier_` | （由 Engine 在 `ProcessKey` 末尾调用，非 Context 内部触发） | （Engine 主动 invoke，供其他组件订阅） | 把未处理按键广播给感兴趣的组件 |

4. **需要观察的现象**：注意 `delete_notifier_` 与 `abort_notifier_` 在 `ConcreteEngine` 里**没有**被 `.connect()`——这说明 Notifier 是「开放给任意组件」的总线，Engine 只订阅它关心的那部分，其余留给 gear 组件按需订阅。
5. **预期结果**：能合上书复述「`PushInput` 改了 input → 触发 update_notifier_ → Engine 重算 Compose」这条最常走的因果链。

#### 4.5.5 小练习与答案

**练习 1**：如果一个开关想「随方案切换而自动重置」，它的名字该怎么起？为什么？

**答案**：以下划线 `_` 开头（如 `_auto_commit`）。因为 `Engine::ApplySchema` 会调 `ClearTransientOptions()`，它用 `lower_bound("_")` 批量删除所有以下划线开头的 option/property（[context.cc:L313-L325](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/context.cc#L313-L325)）。不带下划线的开关（如 `ascii_mode`）是会话级的，换方案后仍保留。

**练习 2**：`get_option("ascii_mode")` 在从未 `set_option` 过时返回什么？这种默认值设计有什么好处？

**答案**：返回 `false`（[context.cc:L292-L298](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/context.cc#L292-L298)）。好处是组件无需在读取前判断「开关是否存在」，统一按「未设置 = 关闭」处理，简化了所有 `get_option` 调用点。

**练习 3**：为什么 `update_notifier_` 是所有 Notifier 中被触发最频繁的？

**答案**：因为它绑定到几乎所有「会影响候选」的状态变化——输入增删、光标移动、高亮切换、清空、回退段等（grep `context.cc` 里的 `update_notifier_(this)` 可见十余处）。设计上是因为这些变化都需要 Engine 重新 `Compose`，集中由 `OnContextUpdate` 入口处理最直接。

---

## 5. 综合实践

**任务**：用一张「状态—信号—反应」流转图，把本讲的 5 个模块串起来，追踪**一次完整的「敲一个字母 → 出候选」回合**在 `Context` 内部的全部状态变化与信号传播。

**步骤**：

1. 假设当前 `input_` 为空、`composition_` 为空（回合刚开始）。
2. 用户按下字母 `n`，被 `speller` Processor 捕获，调用 `ctx->PushInput('n')`。
3. 按 [context.cc:L65-L75](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/context.cc#L65-L75) 推演：`input_` 变为 `"n"`、`caret_pos_` 变为 1，末尾触发 `update_notifier_(this)`。
4. 该信号被 `Engine::OnContextUpdate` 接收（[engine.cc:L76-L77](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L76-L77)），调用 `Compose`（[engine.cc:L154-L169](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L154-L169)）：`comp.Reset(active_input)` → `CalculateSegmentation` → `TranslateSegments`，候选被填进 `composition_`。
5. 前端随后读取 `ctx->GetPreedit()`（[context.cc:L44-L46](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/context.cc#L44-L46)）与候选菜单并显示。
6. **在图上标出**：每一步涉及的 `Context` 字段（`input_`/`caret_pos_`/`composition_`）、触发的 Notifier（`update_notifier_`）、订阅者（`OnContextUpdate`）、最终对外行为（`Compose` 重算）。

**进阶**：继续推演「用户按方向键高亮第 2 个候选」（走 `Highlight` → `update_notifier_` → 但因 Engine 在 `OnSelect` 之外不再做额外事，仅前端重绘）与「用户按回车确认」（走 `ConfirmCurrentSelection` → `select_notifier_` → `OnSelect` → 可能 `Commit` → `commit_notifier_` → `OnCommit` → `sink_`，并把记录压入 `commit_history_`）两条分支。

**预期产出**：一张能解释「为什么 `PushInput` 一行代码最终会让候选列表更新」的完整因果图。这是理解后续 u3-l2/u3-l3（Composition 内部）与 u6（流水线）的入口。

## 6. 本讲小结

- `Context` 是「当前输入会话的瞬时状态容器」，`Engine` 独占一个，持有 `input_`/`caret_pos_`/`composition_`/`commit_history_`/`options_`/`properties_` 六个字段。
- **input 与 caret_pos** 是最上游的原始输入；`PushInput`/`PopInput`/`DeleteInput`/`set_input` 都在末尾触发 `update_notifier_`，这是 Engine 感知「该重新 Compose」的唯一入口。
- **候选编辑操作**（`Select`/`Highlight`/`DeleteCandidate`/`ConfirmCurrentSelection`）围绕最后一个 Segment 改状态并触发 `select_notifier_`/`update_notifier_`/`delete_notifier_`；`Highlight` 在索引未变化时不通知以避免无谓重算。
- **CommitHistory** 是容量 20 的环形 `list<CommitRecord>`，三个 `Push` 重载分别接收通用记录、未处理按键、整段提交；遇 BackSpace/Return 会清空，同类型相邻候选会合并。
- **options / properties** 分两层：以下划线 `_` 开头的是方案级（换方案时被 `ClearTransientOptions` 清掉），其余是会话级；未设置的 option 默认 `false`。
- **8 类 Notifier** 构成内部事件总线；`ConcreteEngine` 订阅其中 5 个（commit/select/update/option/property），`unhandled_key_notifier_` 由 Engine 在按键未命中时主动 invoke，`delete_notifier_`/`abort_notifier_` 留给其他组件按需订阅。

## 7. 下一步学习建议

- **u3-l2 Segmentation 与 Segment**：本讲一直把 `composition_` 当黑盒，下一讲拆开 `Segmentation`（`Segment` 的有序集合）与 `Segment` 的四态状态机（`kVoid/kGuess/kSelected/kConfirmed`）及 `tags`。
- **u3-l3 Composition、Translation 与 Candidate**：进一步看候选如何以 `Translation` 迭代器形式被拉取、`Candidate` 的字段结构。
- **u6-l1 引擎流水线总览**：把本讲的 `update_notifier_ → Compose` 这条因果链放到完整的 `ProcessKey` 主线里，看 Processor→Segmentor→Translator→Filter 如何协同。
- **配套阅读**：可先扫一眼 [src/rime/gear/speller.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/speller.cc) 里 `ctx->PushInput` 的真实调用点，印证本讲 4.2 的描述。
