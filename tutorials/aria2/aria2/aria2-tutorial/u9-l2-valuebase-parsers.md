# ValueBase 与解析器骨架：bencode/JSON 的统一表示

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `ValueBase` 这套「C++ 里的动态类型」包含哪六种类型、树形结构如何用 `unique_ptr` 组织、以及 `downcast<T>()` 如何提供类型安全的访问。
2. 解释 `StructParserStateMachine` 这个与具体格式无关的事件回调契约：九种结构事件、五个回调方法，为什么「词法识别」和「结构装配」要拆成两层。
3. 分别读懂 `BencodeParser` 与 `JsonParser` 两台状态机：它们如何用 `currentState_ + stateStack_` 免疫 TCP 粘包/半包，错误码如何返回，深度如何限制。
4. 跟踪装配层 `ValueBaseStructParserStateMachine`：第二套状态栈 + `XmlRpcRequestParserController` 的框架栈如何把事件流「垒」成 ValueBase 树。
5. 动手写一段使用 `ValueBaseJsonParser` 的测试代码：解析嵌套 JSON、打印结果树、再制造语法错误验证异常路径。

本讲属于「工程实践与二次开发」单元，是 u6-l1（torrent 元数据）、u7-l2（DHT 报文）、u8-l3/u8-l4（RPC 与 WebSocket）三处反复出现的解析基建的「总后台」。

## 2. 前置知识

### 2.1 动态类型（Variant 模式）

C++ 是静态类型语言，但 bencode 与 JSON 的数据是「运行时才知道类型」的：同一个位置可能是字符串、整数、列表或字典。解决办法是经典的 **Variant / 动态类型对象**模式：

- 一个抽象基类 `ValueBase` 表示「任意值」；
- 每种具体类型（字符串、整数……）各派生一个子类；
- 容器（列表/字典）持有的是基类指针，因此可以任意嵌套，形成一棵树。

Python 的 `dict`/`list`、JavaScript 的对象字面量，本质都是这种结构；`ValueBase` 就是 aria2 用 C++ 手工实现的同款。

### 2.2 流式解析与状态机

aria2 的数据经常是「一小块一小块」到达的（RPC 请求体分多个 TCP 段到达、WebSocket 帧逐帧到达、dht.dat 逐块读入）。若等数据到齐再一次性 `json.loads`，既占内存也无法及时反馈错误。所以解析器必须**支持把任意切割的字节流分段喂入**，这就是流式（streaming）解析。

实现流式解析的标准做法是**状态机**：把「解析进行到哪一步」外化成成员变量（当前状态、数字累积值、字符串长度余额……），每喂一段数据就从上次的状态继续。这和 u4-l6 讲过的 `ChunkedDecodingStreamFilter` 是同一哲学：**进度全部外置，天然免疫粘包与半包**。

### 2.3 SAX：事件驱动的解析回调

解析器每识别出一个结构（「一个字典开始了」「一段字符串数据到了」「一个整数结束了」），就调用一个回调通知下游。下游只看到事件流，看不到原始字节。这种风格叫 SAX（Simple API for XML，起源于 XML，但思想通用）。u8-l1 的 Metalink 解析器就是 SAX 风格；本讲的两台解析器同样是。

### 2.4 访问者模式（Visitor）

拿到一棵 `ValueBase` 树后，如何判断某个节点到底是 `String` 还是 `Integer`？C++ 的 `dynamic_cast` 可以做运行时类型识别，但 aria2 选择用**访问者模式**自己实现：节点通过 `accept(visitor)` 反过来调用访问者对应类型的 `visit()` 重载。后面会看到 `downcast<T>()` 就是它的便捷封装。

### 2.5 两种数据格式速览

- **bencode**（u6-l1 讲过）：BitTorrent 的序列化格式，只有四种类型——字符串 `长度:内容`、整数 `i数字e`、列表 `l...e`、字典 `d...e`。没有布尔、没有 null、没有浮点数。字符串按**长度前缀**定界，内容可含任意字节（包括 `e`、`:` 本身）。
- **JSON**：Web 世界通用格式，有字符串（双引号、`\` 转义）、数字（可带小数与指数）、`true`/`false`/`null`、数组 `[...]`、对象 `{...}`。字符串按**引号**定界，支持 `\uXXXX` Unicode 转义。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲角色 |
| --- | --- | --- |
| [src/ValueBase.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ValueBase.h) / src/ValueBase.cc | 动态类型六件套：`String/Integer/Bool/Null/List/Dict` 与 Visitor | 模块 4.1 |
| [src/StructParserStateMachine.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/StructParserStateMachine.h) | 与格式无关的事件契约（纯接口，约 70 行） | 模块 4.2 |
| [src/BencodeParser.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BencodeParser.h) / src/BencodeParser.cc | bencode 词法状态机 | 模块 4.3 |
| [src/JsonParser.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/JsonParser.h) / src/JsonParser.cc | JSON 词法状态机 | 模块 4.4 |
| [src/ValueBaseStructParserStateMachine.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ValueBaseStructParserStateMachine.h) / .cc、src/ValueBaseStructParserStateImpl.h / .cc | 装配层状态机：把事件流垒成 ValueBase 树 | 模块 4.5 |
| [src/XmlRpcRequestParserController.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/XmlRpcRequestParserController.h) / .cc | 框架栈（名字是历史遗留，实际是通用的树装配账本） | 模块 4.5 |
| [src/GenericParser.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/GenericParser.h) | 模板粘合层：词法机 + 装配机 = 产品 | 模块 4.5 |
| [src/ValueBaseBencodeParser.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ValueBaseBencodeParser.h)、[src/ValueBaseJsonParser.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ValueBaseJsonParser.h) | 两个最终产品（各只有一行 typedef） | 模块 4.5 |
| [src/bencode2.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bencode2.cc) | 门面：`bencode2::decode/encode` | 模块 4.5 |
| test/ValueBaseJsonParserTest.cc、test/ValueBaseBencodeParserTest.cc、test/JsonTest.cc、test/Bencode2Test.cc | 测试，实践任务的参考与验证依据 | 各模块实践 |

一张图概括整条链路（自下而上看）：

```text
  字节流（RPC 请求体 / .torrent / DHT 报文 / WebSocket 帧）
        │  parseUpdate(data, size)   ← 可多次调用，任意切割
        ▼
  词法状态机 BencodeParser / JsonParser     ← 识别格式语法，产出「事件」
        │  psm_->beginElement/endElement/charactersCallback/...
        ▼
  StructParserStateMachine（纯接口）
        │
        ▼
  ValueBaseStructParserStateMachine（装配层）  ← 第二套状态栈 + 框架栈
        │  getResult()
        ▼
  std::unique_ptr<ValueBase>（一棵树）
```

## 4. 核心概念与源码讲解

### 4.1 ValueBase：C++ 中的动态类型系统

#### 4.1.1 概念说明

`ValueBase` 是 aria2 全代码共用的「万能值对象」。RPC 的请求与响应、DHT 报文、torrent 字典、BitTorrent 扩展消息、`--save-session` 的会话文件……凡是需要在内存里装一份「结构化数据」的地方，用的都是它。

设计要点：

1. **六个叶子/容器类型**恰好是 bencode 与 JSON 类型的并集：`String`、`Integer`（固定 `int64_t`）、`Bool`、`Null`、`List`、`Dict`。bencode 只用其中四个，JSON 全用。
2. **树形所有权**：`List` 存 `std::deque<std::unique_ptr<ValueBase>>`，`Dict` 存 `std::map<std::string, std::unique_ptr<ValueBase>>`。父节点独占子节点，整棵树随根指针一次析构，无内存泄漏、无引用计数开销。
3. **禁用拷贝**：所有子类的拷贝构造与赋值都被 `= delete`（如 [src/ValueBase.h:L97-L98](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ValueBase.h#L97-L98)）。值只能被移动或经由 `unique_ptr` 转移所有权——这在 C++11 时代的代码库里是常见的性能取舍。
4. **静态工厂 `g()`**：每个类型都有 `String::g(...)`、`Dict::g()` 之类的静态方法直接返回 `unique_ptr`，省去先构造再包指针的两步。

#### 4.1.2 核心流程

六个类型的能力对照：

| 类型 | 底层容器/字段 | 常用读接口 | 常用写接口 | bencode | JSON |
| --- | --- | --- | --- | --- | --- |
| `String` | `std::string` | `s()`、`uc()` | 构造函数 | ✓ | ✓ |
| `Integer` | `int64_t` | `i()` | 构造函数 | ✓ | ✓（含浮点截断） |
| `Bool` | `bool` | `val()` | `gTrue()/gFalse()` | ✗ | ✓ |
| `Null` | （空） | — | `Null::g()` | ✗ | ✓ |
| `List` | `deque<unique_ptr<ValueBase>>` | `get(i)`、`operator[]`、迭代器 | `append()`、`operator<<` | ✓ | ✓ |
| `Dict` | `map<string, unique_ptr<ValueBase>>` | `get(key)`、`operator[]`、`containsKey()` | `put()`、`popValue()` | ✓ | ✓ |

两个容易踩的语义细节：

- **`Dict` 是有序的**：底层是 `std::map`，遍历永远按 key 的字典序。bencode 规范本身要求键有序，所以「解析再重编码」是确定性的——这正是 u6-l1 讲过 infohash 能「重编码后 SHA-1 不变」的前提。
- **`Dict::put` 对重复键是覆盖，不是报错**（见下文源码精读）。

#### 4.1.3 源码精读

**基类与访问者**。`ValueBase` 本体只有一个虚函数 `accept`：

- [src/ValueBase.h:L51-L56](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ValueBase.h#L51-L56) —— 抽象基类，`accept(visitor)` 是唯一的公开行为。
- [src/ValueBase.h:L65-L74](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ValueBase.h#L65-L74) —— `ValueBaseVisitor` 为六种类型各声明一个 `visit` 重载。

**类型安全访问器 `downcast<T>`**。这是本讲学习目标里的「类型安全访问器」，实现极短：

- [src/ValueBase.h:L326-L351](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ValueBase.h#L326-L351) —— `DowncastValueBaseVisitor<T>` 只在 `visit(const T&)` 里记录指针，其余类型全空实现；自由函数 `downcast<T>(v)` 把节点喂给该访问者，类型不匹配时返回 `nullptr`。用法习惯是「downcast 后立刻判空」，测试代码里随处可见（如 [test/ValueBaseJsonParserTest.cc:L139-L144](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/ValueBaseJsonParserTest.cc#L139-L144)）。

**容器的所有权结构**：

- [src/ValueBase.h:L175-L201](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ValueBase.h#L175-L201) —— `List` 的 `ValueType` 是 `std::deque<std::unique_ptr<ValueBase>>`；`get(index)` 返回裸指针（借用，不转移所有权）。
- [src/ValueBase.h:L247-L266](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ValueBase.h#L247-L266) —— `Dict` 的 `ValueType` 是 `std::map`；`get(key)` 查不到返回 `nullptr`，所以「键不存在」与「值是 Null 类型」是两回事，判断时要用 `containsKey` 或先判 `get` 的返回值。

**`Dict::put` 的覆盖语义**：

- [src/ValueBase.cc:L163-L170](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ValueBase.cc#L163-L170) —— 先 `insert`，若 `r.second == false`（键已存在）则把旧值替换为新值。也就是说，JSON 输入里出现重复键时，后者胜出且不报错。

**序列化的对称性**：`bencode2::encode` 与 `json::encode` 都是用 Visitor 遍历树输出文本，例如 [src/bencode2.cc:L78-L93](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bencode2.cc#L78-L93) 中 `BencodeValueBaseVisitor` 对 `String` 输出 `长度:内容`、对 `Integer` 输出 `i数字e`。**解析（本讲主线）与序列化共用同一棵 ValueBase 树**，构成了「解码 ↔ 内存表示 ↔ 编码」的闭环。

#### 4.1.4 代码实践

**实践目标**：用手工构造的 ValueBase 树验证「树形结构 + 有序 Dict + encode 对称性」三件事。

**操作步骤**（源码阅读 + 可选上机）：

1. 阅读 [test/JsonTest.cc:L26-L45](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/JsonTest.cc#L26-L45)：测试先构造 `dict(name=aria2, loc=80000, files=[aria2c], attrs={license=GPL})`，再断言 `json::encode` 的输出。
2. 注意断言里的键顺序是 `attrs, files, loc, name`——**不是**插入顺序 `name, loc, files, attrs`。这是 `std::map` 字典序的直接证据。
3. 阅读对称测试 [test/Bencode2Test.cc:L35-L42](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/Bencode2Test.cc#L35-L42)，同样这棵树，bencode 编码为 `d5:attrsd7:license3:GPLe5:filesl6:aria2ce3:loci80000e4:name5:aria2e`。
4. （可选上机）把下面断言抄进 `test/JsonTest.cc` 的 `testEncode` 末尾并运行 `make check`（示例代码）：

```cpp
auto d = Dict::g();
d->put("b", String::g("2"));
d->put("a", String::g("1"));
d->put("a", String::g("override")); // 重复键，覆盖
CPPUNIT_ASSERT_EQUAL(std::string("{\"a\":\"override\",\"b\":\"2\"}"),
                     json::encode(d.get()));
```

**需要观察的现象**：编码输出键有序；`a` 的值是 `override`。

**预期结果**：断言通过。若你在本机跑了 `make check`，`test/aria2c` 二进制会构建并执行全部用例（耗时较长），失败行会以 `JsonTest::testEncode` 名义打印。未上机则为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `List::get(index)` 返回裸指针 `ValueBase*`，而不是 `std::unique_ptr<ValueBase>`？

**答案**：`get` 是「借用」语义——只读访问，不改变树的所有权。若返回 `unique_ptr`，容器就被迫交出所有权，访问一次就拆一棵子树。需要转移所有权时另有专门接口（如 [src/ValueBase.h:L275](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ValueBase.h#L275) 的 `Dict::popValue`）。

**练习 2**：JSON 输入 `{"a":1,"a":2}` 解析后 `Dict::size()` 是多少？`get("a")` 的值呢？

**答案**：`size()` 为 1；值为 `Integer(2)`。因为 [src/ValueBase.cc:L163-L170](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ValueBase.cc#L163-L170) 的 `put` 对已存在键做替换。

**练习 3**：`downcast<String>(dict->get("missing"))` 返回什么？

**答案**：`dict->get` 先因键不存在返回 `nullptr`，`downcast` 对空指针直接返回 0（见 [src/ValueBase.h:L341-L351](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ValueBase.h#L341-L351) 的 `if (v)` 分支）。所以判空既是类型检查也是存在性检查。

### 4.2 StructParserStateMachine：与格式无关的事件契约

#### 4.2.1 概念说明

前文链路图里，词法层（认字节）和装配层（建树）之间隔着一个纯接口 `StructParserStateMachine`。为什么要隔这一层？

1. **复用装配逻辑**。bencode 和 JSON 的「字典套列表套字符串」结构语义完全一致，差异只在字法（`d` vs `{`、长度前缀 vs 引号）。把「开始一个字典」「收到一段字符」抽象成事件后，装配层只需写一遍。
2. **可替换的下游**。理论上你可以实现一个「边解析边打印」或「边解析边写数据库」的状态机挂到同一台词法机后面，而不碰解析代码。文件头注释就把它定位为「structured data format (e.g., JSON, Bencode) 的流式解析器接口」。
3. **测试解耦**。两台词法机可共用一套「事件序列断言」的测试手法。

当前代码树中，这个接口只有一个正式实现：`ValueBaseStructParserStateMachine`（模块 4.5）。XML-RPC 那套 `XmlRpcRequestParserStateMachine` 走的是 `ParserStateMachine`（u8-l1 讲过的 SAX 接口），并不实现本接口——但它**复用了同一个框架栈控制器**，所以代码里才会出现 `rpc::XmlRpcRequestParserController` 被 ValueBase 装配层持有的「跨界」现象。

#### 4.2.2 核心流程

契约一共九种结构事件 + 五个回调：

| 回调 | 含义 | bencode 触发点 | JSON 触发点 |
| --- | --- | --- | --- |
| `beginElement(DICT_T)` | 字典开始 | `d` | `{` |
| `endElement(DICT_T)` | 字典结束 | 配对的 `e` | `}` |
| `beginElement(DICT_KEY_T)` | 键字符串开始 | 字典内遇到长度前缀 | `"` (键位置) |
| `endElement(DICT_KEY_T)` | 键字符串结束 | 键内容读满 | 键的闭引号 |
| `beginElement(DICT_DATA_T)` | 成员值开始 | 键结束后立即 | `:` 之后 |
| `endElement(DICT_DATA_T)` | 成员值结束 | 值完整 | 值完整 |
| `beginElement(ARRAY_T)` / `endElement(ARRAY_T)` | 列表开始/结束 | `l` / `e` | `[` / `]` |
| `beginElement(ARRAY_DATA_T)` / `endElement(ARRAY_DATA_T)` | 元素开始/结束 | 每个元素 | 每个元素 |
| `beginElement(STRING_T)` / `endElement(STRING_T)` | 字符串值开始/结束 | `数字:` / 读满长度 | `"` / `"` |
| `beginElement(NUMBER_T)` / `endElement(NUMBER_T)` | 数字开始/结束 | `i` / `e` | 首字符 / 数字结束 |
| `beginElement(BOOL_T)` / `endElement(BOOL_T)` | 布尔开始/结束 | （无） | `t` 或 `f` / 字面量读完 |
| `beginElement(NULL_T)` / `endElement(NULL_T)` | null 开始/结束 | （无） | `n` / 字面量读完 |
| `charactersCallback(data,len)` | 一段原始字符数据 | 字符串/键的正文（可能分多次） | 同左 |
| `numberCallback(number,frac,exp)` | 一个完整数字 | 整数（frac=exp=0） | 可带小数/指数 |
| `boolCallback(bval)` | 一个布尔 | （无） | `true`/`false` 读完 |
| `reset()` | 复位全部状态 | — | — |

三条使用纪律：

- `begin/end` 必须严格配对且可嵌套，形成「括号序列」；
- `charactersCallback` 对同一字符串可能被调用多次（数据分段到达），下游必须做**追加**而非覆盖；
- `DICT_KEY_T` 与 `STRING_T` 共用 `charactersCallback`，下游靠「当前处于哪个事件里」区分键与普通字符串。

#### 4.2.3 源码精读

整个契约只有一个头文件、零实现代码：

- [src/StructParserStateMachine.h:L42-L52](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/StructParserStateMachine.h#L42-L52) —— `StructElementType` 枚举定义九种事件类型。注意它刻意细到「键」「成员值」「数组元素」三个层级事件（`DICT_KEY/DICT_DATA/ARRAY_DATA`），这让装配层不必自己推断「这个字符串是键还是值」。
- [src/StructParserStateMachine.h:L56-L66](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/StructParserStateMachine.h#L56-L66) —— 接口本体：`beginElement/endElement/reset/charactersCallback/numberCallback/boolCallback` 六个纯虚函数。

词法机如何消费这个契约？以 bencode 为例：

- [src/BencodeParser.cc:L367-L401](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BencodeParser.cc#L367-L401) —— `runBeginCallback` 只做一件事：`psm_->beginElement(elementType)`。注意函数体内那大段被注释掉的 `switch`——那是作者调试时用过的「事件打印器」，本讲实践会请你自己写一个活的事件打印器。
- [src/BencodeParser.cc:L444-L447](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BencodeParser.cc#L444-L447) —— `runNumberCallback(number, 0, 0)`：bencode 没有小数与指数，后两个参数恒为 0。这正是「接口取两格式并集」的体现——JSON 侧会传真实的 frac/exp（[src/JsonParser.cc:L756-L759](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/JsonParser.cc#L756-L759)）。

#### 4.2.4 代码实践

**实践目标**：亲眼看到「字节流 → 事件流」的转换过程，验证 4.2.2 的表格。

**操作步骤**：

1. 写一个最小的「事件打印机」状态机（示例代码，可直接放进 `test/` 目录新建文件或临时替换某测试）：

```cpp
// 示例代码：打印 StructParserStateMachine 事件流
#include <cstdio>
#include "StructParserStateMachine.h"

class EventPrinter : public aria2::StructParserStateMachine {
public:
  void beginElement(int t) override
  {
    printf("begin  %s\n", name(t));
  }
  void endElement(int t) override { printf("end    %s\n", name(t)); }
  void reset() override { printf("reset\n"); }
  void charactersCallback(const char* data, size_t len) override
  {
    printf("chars  [%zu bytes]\n", len);
  }
  void numberCallback(int64_t n, int frac, int exp) override
  {
    printf("number %lld (frac=%d exp=%d)\n", (long long)n, frac, exp);
  }
  void boolCallback(bool b) override { printf("bool   %d\n", (int)b); }

private:
  const char* name(int t)
  {
    switch (t) {
    case STRUCT_DICT_T: return "DICT";
    case STRUCT_DICT_KEY_T: return "DICT_KEY";
    case STRUCT_DICT_DATA_T: return "DICT_DATA";
    case STRUCT_ARRAY_T: return "ARRAY";
    case STRUCT_ARRAY_DATA_T: return "ARRAY_DATA";
    case STRUCT_STRING_T: return "STRING";
    case STRUCT_NUMBER_T: return "NUMBER";
    case STRUCT_BOOL_T: return "BOOL";
    case STRUCT_NULL_T: return "NULL";
    }
    return "?";
  }
};
```

2. 把它与词法机接线（示例代码）：

```cpp
// 示例代码
EventPrinter printer;
aria2::bittorrent::BencodeParser parser(&printer);
const char src[] = "d3:fooi123ee";
printf("consumed=%zd\n",
       parser.parseUpdate(src, sizeof(src) - 1));
parser.parseFinal(0, 0);
```

3. 若不想新文件，也可以直接取消 [src/BencodeParser.cc:L369-L399](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BencodeParser.cc#L369-L399) 与 [L404-L435](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BencodeParser.cc#L404-L435) 两段注释的注释符，`make -C src` 重编后跑 `make -C test check`，观察 `ValueBaseBencodeParserTest` 输出前的事件行（验完记得还原源码）。

**需要观察的现象**：对 `d3:fooi123ee` 应依次打出 `begin DICT → begin DICT_KEY → chars [3 bytes] → end DICT_KEY → begin DICT_DATA → begin NUMBER → number 123 → end NUMBER → end DICT_DATA → end DICT`。

**预期结果**：事件序列严格嵌套配对，与 4.2.2 表格一致；`parseUpdate` 返回消费的字节数 12。具体控制台输出「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `charactersCallback` 的参数是 `(const char* data, size_t len)`，而不是直接传 `std::string`？

**答案**：回调内容可能含 `\0`（bencode 字符串是任意二进制），`std::string` 虽也能存 `\0`，但指针 + 长度更明确地表达「二进制安全」，也避免了每次回调构造临时 string 的开销。分段到达时同一字符串会触发多次回调，下游需要追加。

**练习 2**：`DICT_DATA` 事件存在的意义是什么？只靠 `DICT_T + STRING/NUMBER` 不足以装配字典吗？

**答案**：`DICT_DATA_T` 明确划出「一个成员值的边界」。装配层在 `begin(DICT_DATA)` 时压栈、在 `end(DICT_DATA)` 时把「刚才完成的值 + 之前记住的键」写进父字典（见 4.5 的 `popStructFrame`）。没有它，装配层就得自己判断「一个值算不算结束」，嵌套结构（如值本身是列表）会变得歧义。

**练习 3**：`boolCallback` 为什么不与 `numberCallback` 合并（比如约定 number 0/1 表示布尔）？

**答案**：类型系统里 `Bool` 与 `Integer` 是不同节点（JSON 里 `true` 与 `1` 语义不同）。分开回调让装配层生成正确的 `Bool::gTrue()/gFalse()` 节点，也保持了 bencode（无布尔）实现的清晰——它的 `runNumberCallback` 不需要任何特判。

### 4.3 BencodeParser：长度前缀格式的词法状态机

#### 4.3.1 概念说明

`BencodeParser`（命名空间 `aria2::bittorrent`）把 bencode 字节流翻译成 4.2 的事件流。它面对的格式特性决定了状态机形态：

- 字符串按**长度前缀**定界（`3:foo`），读完长度指定的字节数即结束——即使内容里出现 `e`、`:` 也不会误判，天然二进制安全；
- 四种类型各有一个起始字符（`d/l/i/数字`），终结符统一是 `e`（仅容器与整数）；
- 没有空白、没有转义、没有布尔/null——语法极简，所以状态数只有 11 个。

工程上它还要处理两件「现实世界」的事：输入可能任意分段（粘包/半包）、恶意或损坏的输入可能嵌套过深（防栈溢出/资源耗尽）。

#### 4.3.2 核心流程

11 个状态（[src/BencodeParser.cc:L47-L60](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BencodeParser.cc#L47-L60)）：

| 状态 | 含义 | 迁出条件 |
| --- | --- | --- |
| `INITIAL` / `VALUE` | 期待一个值 | `d/l/i/数字` 分别进入字典/列表/整数/字符串长度 |
| `DICT_KEY` | 期待字典键或 `e` | `e`→字典结束；否则压栈进入字符串长度 |
| `DICT_VAL`（栈上） | 键已结束，期待值 | 值完成后弹栈回 `DICT_KEY` |
| `LIST` | 列表体内 | `e`→列表结束；否则压栈进入 `VALUE` |
| `STRING_LEN` | 累积长度数字 | 遇 `:` 进入 `STRING` |
| `STRING` | 消耗正文 | 计数归零→字符串结束 |
| `NUMBER_SIGN` / `NUMBER` | 整数符号与数字 | 非 `e` 结束符→报错 |
| `FLOAT_NUMBER_IGNORE` | 容错跳过伪浮点（见下） | 遇 `e` 结束整数 |
| `FINISH` / `ERROR` | 终态 | — |

主循环骨架（伪代码）：

```text
parseUpdate(data, size):
  若 currentState 是 FINISH: return 0          # 已完成，多余输入不消费
  若 currentState 是 ERROR:  return lastError_ # 粘住错误，后续调用直接返回
  for 每个字节 c:
    switch currentState:
      ...识别 → 改状态 → runXxxCallback(psm_)...
  return 已消费字节数 i
parseFinal(data, len):
  rv = parseUpdate(data, len)
  若 rv >= 0 且状态仍停留在中间态: rv = ERR_PREMATURE_DATA
  return rv
```

「一个值算结束」的统一出口是 `onValueEnd()`，它按**状态栈顶**决定回到哪里——栈里记的正是「我是谁的子值」。

#### 4.3.3 源码精读

**值的四路分派**：

- [src/BencodeParser.cc:L103-L135](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BencodeParser.cc#L103-L135) —— 在 `INITIAL/VALUE`（或从 `LIST` 落下来）处，`d` 触发 `begin(DICT_T)`、`l` 触发 `begin(ARRAY_T)`、`i` 进入数字累积并 `begin(NUMBER_T)`、数字触发 `begin(STRING_T)`；其他字符直接 `ERR_UNEXPECTED_CHAR_BEFORE_VAL`。

**长度前缀字符串的两段状态**：

- [src/BencodeParser.cc:L151-L178](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BencodeParser.cc#L151-L178) —— `STRING_LEN`：内层循环一口气吃掉所有数字（每次乘 10 前用 \((INT64\_MAX - d)/10 < strLength\_\) 预判溢出），遇 `:` 切换到 `STRING`。若本段数据用完还没见到 `:`（`j == size`），`i = j - 1` 挂起，等下一次 `parseUpdate` 从 `STRING_LEN` 继续——**半包安全**的关键就在这里。
- [src/BencodeParser.cc:L179-L188](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BencodeParser.cc#L179-L188) —— `STRING`：本段最多消费 `min(剩余字节, strLength_)`，差额留给下次调用；计满即 `onStringEnd()`。正文逐段经 `charactersCallback` 外送，装配层负责追加。

**伪浮点容错**（真实世界 hack）：

- [src/BencodeParser.cc:L226-L237](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BencodeParser.cc#L226-L237) —— 源码注释说明：某些 torrent 生成器会在整数字段写出 `-1.134E+3` 这类科学计数法浮点。解析器不报错，而是清零数值、进入 `FLOAT_NUMBER_IGNORE`。
- [src/BencodeParser.cc:L253-L266](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BencodeParser.cc#L253-L266) —— 跳过所有数字/`.`/`E`/`+`/`-`，直到遇到 `e` 以 `onNumberEnd()` 收场——此时 `number_` 已被清零，所以这类输入最终得到 `Integer(0)`。

**栈驱动的值归位**：

- [src/BencodeParser.cc:L321-L345](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BencodeParser.cc#L321-L345) —— `onValueEnd()` 看栈顶：刚完成的是**键**（`DICT_KEY` 在顶）→ 弹出、压入 `DICT_VAL`、`begin(DICT_DATA_T)`；刚完成的是**成员值**（`DICT_VAL` 在顶）→ `end(DICT_DATA_T)`、弹栈、回 `DICT_KEY`；刚完成的是**列表元素**（`LIST` 在顶）→ `end(ARRAY_DATA_T)`、弹栈、留在 `LIST`；栈底 `FINISH` → 整个输入完成。
- [src/BencodeParser.cc:L295-L300](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BencodeParser.cc#L295-L300) —— `onStringEnd` 用 `stateTop() == BENCODE_DICT_KEY` 区分「这是键」还是「这是普通字符串值」，对应发出 `DICT_KEY_T` 或 `STRING_T` 的 end 事件。

**防御性限制**：

- [src/BencodeParser.cc:L347-L356](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BencodeParser.cc#L347-L356) —— 嵌套深度上限 50，超出返回 `ERR_STRUCTURE_TOO_DEEP`。防止畸形输入构造超深嵌套拖垮装配层。
- [src/BencodeParser.cc:L272-L282](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BencodeParser.cc#L272-L282) —— `parseFinal` 若发现结束时还停在中间状态（如 `d3:foo` 没有收尾 `e`），返回 `ERR_PREMATURE_DATA`；另外注意 `FINISH` 之后多余的字节被忽略（`parseUpdate` 直接返回 0），这也解释了 `test/Bencode2Test` 允许「值后跟垃圾」的行为。

错误码全集见 [src/BencodeParser.h:L48-L57](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BencodeParser.h#L48-L57)，全部为负数，`ssize_t` 返回值即错误码——**用一个返回值同时表达「消费了多少字节」与「出了什么错」**。

#### 4.3.4 代码实践

**实践目标**：验证「长度前缀定界 + 伪浮点容错」两个行为。

**操作步骤**：

1. 打开 [test/ValueBaseBencodeParserTest.cc:L82-L86](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/ValueBaseBencodeParserTest.cc#L82-L86)，读断言：输入 `i+343243.342E-1333e` 期望 `Integer(0)`。
2. 手工跟踪这 18 个字节在 [src/BencodeParser.cc:L208-L266](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BencodeParser.cc#L208-L266) 的走向：`i` → `NUMBER_SIGN` 吃掉 `+` → `NUMBER` 吃掉 `343243` 后看到 `.`（非 `e` 的数字相关字符）→ 清零进入 `FLOAT_NUMBER_IGNORE` → 吞掉 `342E-1333` → 最后的 `e` 触发 `onNumberEnd`，此时 `number_ = 0`。
3. 再验证定界：输入 `4:ele` 是合法字符串 `"ele"`（长度 4），不是 `e` 终结符——按 `STRING` 状态的计数逻辑推一遍（[L179-L188](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BencodeParser.cc#L179-L188)）。
4. （可选上机）把 `4:ele`、`i-1.5E3e` 追加进 `testParseUpdate` 各写一条断言，`make -C test check` 验证。

**需要观察的现象**：浮点输入解析为 0 而不报错；`4:ele` 得到字符串 `ele`（尾部的 e 属于内容）。

**预期结果**：与断言一致；未上机部分「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：把 `d3:fooi123ee` 从中间劈开，先喂 `d3:fo`，再喂 `oi123ee`，解析结果会变吗？

**答案**：不会。所有进度（当前状态、长度余额、数字累积值）都是成员变量，第一次调用挂在 `STRING` 状态、`strLength_` 剩 1；第二次调用继续消费 `o` 后计满，正常发出键事件。这正是流式设计的目的（可对照 [src/WebSocketSession.cc:L321](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketSession.cc#L321) 的真实增量用法）。

**练习 2**：`parseUpdate` 出错后再喂新数据会发生什么？

**答案**：状态停在 `ERROR`，后续任何 `parseUpdate` 直接返回 `lastError_`（[src/BencodeParser.cc:L83-L85](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BencodeParser.cc#L83-L85)）。要继续使用必须 `reset()`。

**练习 3**：为什么溢出检查写成 `(INT64_MAX - (data[j]-'0'))/10 < strLength_` 而不是先乘后比？

**答案**：先乘可能已经溢出，溢出后的比较毫无意义（UB）。该式在乘 10 之前判断「再进一位是否会超上限」，等价于要求 \( strLength\_ \le (INT64\_MAX - d)/10 \)，是标准的溢出预判写法。JSON 侧同款（[src/JsonParser.cc:L396-L403](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/JsonParser.cc#L396-L403)）。

### 4.4 JsonParser：带转码与数字细节的状态机

#### 4.4.1 概念说明

`JsonParser`（命名空间 `aria2::json`）与 `BencodeParser` 是同构的姐妹机：同样的 `parseUpdate/parseFinal/reset` 三件套、同样的双栈结构、同样的 50 层深度上限、同样的 `onValueEnd` 归位逻辑。差异全部来自 JSON 语法本身：

- 引号定界 + `\` 转义（8 种短转义 + `\uXXXX`）；
- `true/false/null` 三个字面量要逐字符校验；
- 数字有符号、小数、指数三段；
- 对象有 `:` 与 `,` 两个标点状态、数组有 `,` 标点状态；
- 允许（其实过度允许，见练习）前导零；空白可出现在几乎所有缝隙。

于是状态数膨胀到 22 个，还需要两个额外的码点寄存器（`codepoint_/codepoint2_`）处理 UTF-16 代理对。

#### 4.4.2 核心流程

状态全集（[src/JsonParser.cc:L47-L69](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/JsonParser.cc#L47-L69)）按职责分组：

| 分组 | 状态 | 说明 |
| --- | --- | --- |
| 顶层/元素 | `VALUE`、`FINISH`、`ERROR` | 与 bencode 同 |
| 对象 | `OBJECT_KEY`、`OBJECT_VAL`、`OBJECT_SEP` | 期待键 / `:` / `,` 或 `}` |
| 数组 | `ARRAY`、`ARRAY_SEP` | 期待元素或 `]` / `,` 或 `]` |
| 字符串 | `STRING`、`STRING_ESCAPE`、`STRING_UNICODE`、三个 `LOW_SURROGATE*` | 正文 / 转义 / \u 转码 / 代理对 |
| 数字 | `NUMBER`、`NUMBER_FRAC`、`NUMBER_EXP_SIGN`、`NUMBER_EXP` | 整数段 / 小数段 / 指数符号 / 指数段 |
| 字面量 | `TRUE`、`FALSE`、`NULL` | 逐字符匹配 |

`\uXXXX` 转码规则（RFC 3629 UTF-8）：

- 码点 \( \le 0x7F \)：1 字节；
- \( \le 0x7FF \)：2 字节 `110xxxxx 10xxxxxx`；
- 其他（含 BMP 内三字节段）：3 字节；
- 若是**高代理项**（0xD800–0xDBFF），必须后跟 `\uDC00`–`\uDFFF` 的低代理项，合并公式为：

\[ cp = 0x10000 + (H - 0xD800) \times 0x400 + (L - 0xDC00) \]

结果编码为 4 字节 UTF-8。低代理项缺失或不合法即报 `ERR_INVALID_UNICODE_POINT`。

数字的取舍：`frac_` 最多累积 8 位有效数字、`exp_` 超过 18 直接报错（[src/JsonParser.cc:L437-L496](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/JsonParser.cc#L437-L496)）；但装配层最终**丢弃 frac/exp，只留整数段**（见 4.5）。JSON 浮点数在 aria2 里一律截断成整型。

#### 4.4.3 源码精读

**值的七路分派**（比 bencode 多出字符串/布尔/null 三路）：

- [src/JsonParser.cc:L129-L178](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/JsonParser.cc#L129-L178) —— `{`/`[`/`"`/`-`/数字/`t`/`f`/`n` 分别进入对应状态并发出 begin 事件；既非值字符也非空白则 `ERR_UNEXPECTED_CHAR_BEFORE_VAL`。空白判定用 `isSpace`（LWS + CRLF，[L96-L98](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/JsonParser.cc#L96-L98)）。

**字符串正文与转义**：

- [src/JsonParser.cc:L285-L304](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/JsonParser.cc#L285-L304) —— `STRING` 状态内层循环一口气扫到 `\\` 或 `"` 为止，整段正文一次性 `charactersCallback`（减少回调次数）；遇到 `\` 进入 `ESCAPE`。
- [src/JsonParser.cc:L305-L338](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/JsonParser.cc#L305-L338) —— 8 种短转义各自产出对应字符（`\n` → 换行等）；`\u` 进入 UNICODE 状态；其余字符「转义自己」（`\/` → `/`）。

**\u 转码与代理对**：

- [src/JsonParser.cc:L622-L654](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/JsonParser.cc#L622-L654) —— `consumeUnicode`：4 个十六进制位收齐后，若是高代理项则转入等待低代理项的三连状态；否则按 1/2/3 字节模板手工编码 UTF-8 输出。
- [src/JsonParser.cc:L656-L677](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/JsonParser.cc#L656-L677) —— `consumeLowSurrogate`：校验低代理项区间、套用上面的合并公式、输出 4 字节 UTF-8。测试 [test/ValueBaseJsonParserTest.cc:L104-L112](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/ValueBaseJsonParserTest.cc#L104-L112) 断言 `𤭢` 得到字节 `F0 A4 AD A2`，可以拿来自查手工位运算。

**对象/数组的标点状态机**：

- [src/JsonParser.cc:L217-L267](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/JsonParser.cc#L217-L267) —— `OBJECT_KEY`（期待 `"` 或 `}`）/ `OBJECT_VAL`（期待 `:`）/ `OBJECT_SEP`（期待 `,` 或 `}`）三状态循环，任何缝隙里的非空白非法字符都有专属错误码（`ERR_UNEXPECTED_CHAR_BEFORE_OBJ_KEY/OBJ_VAL/OBJ_SEP`），错误定位粒度比 bencode 细得多。
- [src/JsonParser.cc:L578-L600](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/JsonParser.cc#L578-L600) —— `onValueEnd` 与 bencode 版逐行对仗：键完成 → `OBJECT_VAL`；成员值完成 → 发 `end(DICT_DATA)` 进 `OBJECT_SEP`；元素完成 → 发 `end(ARRAY_DATA)` 进 `ARRAY_SEP`。对照 [src/BencodeParser.cc:L321-L345](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BencodeParser.cc#L321-L345) 阅读，能清楚看到「同一骨架、不同编码」。

**浮点参数的传递**：

- [src/JsonParser.cc:L547-L552](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/JsonParser.cc#L547-L552) —— `onNumberEnd` 把 `numberSign_*number_`、`frac_`、`expSign_*exp_` 三件全数交给 `numberCallback`。下游（4.5）只取整数段。

**解析完成后的尾巴检查**：

- [src/JsonParser.cc:L517-L527](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/JsonParser.cc#L517-L527) —— `parseFinal` 要求结束状态必须是 `FINISH`，否则 `ERR_PREMATURE_DATA`。测试 [test/ValueBaseJsonParserTest.cc:L205-L212](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/ValueBaseJsonParserTest.cc#L205-L212) 同时印证：`[]trail` 不报错但 `error == 2`（值完成后只消费了前 2 字节，尾部垃圾被忽略）。

#### 4.4.4 代码实践

**实践目标**：验证 `\u` 转码、代理对与错误路径。

**操作步骤**：

1. 手工计算 `"¢"`（¢，U+00A2）的 UTF-8 编码：\( 0xA2 = 10100010_2 \)，11 位 → 2 字节模板 `110xxxxx 10xxxxxx`，填入得 `C2 A2`。对照 [test/ValueBaseJsonParserTest.cc:L95-L102](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/ValueBaseJsonParserTest.cc#L95-L102) 断言中的 `¢`。
2. 手工计算代理对 `𤭢`：\( H=0xD852,\ L=0xDF62 \)，\( cp = 0x10000 + 0x52 \times 0x400 + 0x362 = 0x24B62 \)，4 字节 UTF-8 为 `F0 A4 AD A2`——与 [test/ValueBaseJsonParserTest.cc:L104-L112](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/ValueBaseJsonParserTest.cc#L104-L112) 的字节数组完全一致。
3. 阅读 [test/ValueBaseJsonParserTest.cc:L215-L283](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/ValueBaseJsonParserTest.cc#L215-L283) 的 `checkDecodeError` 系列：30 余个坏输入（孤立代理项、缺冒号、深度 51……），断言统一是「返回空指针 + error < 0」。
4. （可选上机）向该函数追加一条 `checkDecodeError("{\"a\":1,}");`（尾逗号），预测 `OBJECT_KEY` 状态遇 `}` 会怎样，再跑 `make -C test check` 验证你的预测。

**需要观察的现象**：尾逗号输入走 `OBJECT_KEY` 的 `}` 分支直接 `onObjectEnd`——注意！这意味着 aria2 接受尾逗号（宽松行为）。如果实验结果与此不符，请以实际输出为准（「待本地验证」）。

**预期结果**：步骤 1/2 的手工字节与测试断言一致；坏输入全部 `error < 0`。

#### 4.4.5 小练习与答案

**练习 1**：JSON 输入 `[1.9, -2.9]` 解析后 List 里是什么？

**答案**：`Integer(1)` 与 `Integer(-2)`。`frac_` 被解析但装配层忽略（[src/ValueBaseStructParserStateImpl.cc:L123-L128](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ValueBaseStructParserStateImpl.cc#L123-L128) 注释 `TODO Ignore frac and exp`），且是**截断**不是四舍五入：符号在 `numberSign_` 上单独维护，`-2.9` 的整数段是 2、符号 -1。

**练习 2**：为什么 `[00]` 不报错（测试 L271-272 的 TODO 注释）？

**答案**：`JSON_VALUE` 的 default 分支只判断「是不是数字」，不校验前导零（[src/JsonParser.cc:L166-L176](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/JsonParser.cc#L166-L176)）；第一个 0 之后解析器已进入 `NUMBER`，第二个 0 继续累积成 0。这是对 JSON 规范的已知偏离，源码以 TODO 形式承认。

**练习 3**：两台解析器都有 `stateStack_` 且限深 50。装配层（下一模块）也有一套栈，为什么不共用一套？

**答案**：职责不同。词法栈记录「语法上下文」（我在对象里还是数组里、期待键还是值），int 枚举即可；装配栈记录「树构建进度」（当前正在构建哪种节点、何时挂到父亲上），元素是状态对象指针。分层后词法机可换任意下游（比如 4.2 的事件打印机），装配机也可服务任意词法格式。

### 4.5 装配层：ValueBaseStructParserStateMachine 与 GenericParser

#### 4.5.1 概念说明

事件流要变成 ValueBase 树，需要一个「听到 begin 就准备好容器、听到 characters 就攒字符串、听到 end 就把完成的值挂到父亲身上」的组件。这就是 `StructParserStateMachine` 接口的正式实现 `ValueBaseStructParserStateMachine`。

它内部有两套关键数据结构，加上第三套在控制器里，共「三本账」：

1. **装配状态栈 `stateStack_`**（`std::stack<ValueBaseStructParserState*>`）：记录「当前正在构建什么」。九个单例状态对象（无状态、全局复用）按事件进出栈，规则是 **begin 压一个、end 弹一个**——和 u8-l1 Metalink 解析器的栈式状态机同一手法。
2. **会话数据 `sessionData_`**：一个 `str` 字符串桶 + 一个 `NumberData` + 一个 `bool`，暂存「正在累积的标量」。`charactersCallback` 只管往桶里追加，end 事件到来时由对应状态把桶整体变成 `String`/键名。
3. **框架栈（在 `XmlRpcRequestParserController` 里）**：一个 `std::stack<StateFrame>`，每帧持有「正在构建的容器值 + 待挂的键名」。进入嵌套容器时 `pushFrame` 保现场，容器完成时弹帧并把当前帧挂到父帧——**树形的递归结构被显式地摊平成栈操作**。

最后由 `GenericParser<Parser, ParserStateMachine>` 模板把词法机与装配机焊成最终产品 `ValueBaseBencodeParser` / `ValueBaseJsonParser`。

#### 4.5.2 核心流程

以解析 `{"foo":[1]}` 为例（装配层视角）：

```text
begin DICT        → valueState: 当前帧的值 = new Dict；压 dictState
begin DICT_KEY    → dictState: pushFrame()（保 Dict 现场）；清空 str 桶；压 dictKeyState
  characters "foo"→ str 桶追加
end DICT_KEY      → dictKeyState: 键名 = str("foo")；弹栈
begin DICT_DATA   → dictState: 压 dictDataState
  begin ARRAY     → dictDataState(继承 valueState 行为): 当前帧的值 = new List；压 arrayState
    begin ARRAY_DATA → arrayState: pushFrame()（保 List 现场）；压 arrayDataState
      begin NUMBER   → 压 numberState；清空 number
      number 1        → 记入 number 桶
      end NUMBER      → numberState: 当前帧的值 = Integer(1)；弹栈
    end ARRAY_DATA   → arrayDataState: popArrayFrame() → List.append(Integer(1))；弹栈
  end ARRAY       → arrayState: end 为空操作；弹栈
end DICT_DATA     → dictDataState: popDictFrame() → Dict["foo"] = List；弹栈
end DICT          → dictState: end 为空操作；弹栈
getResult()       → popCurrentFrameValue() → 整棵 Dict 树
```

要诀：**值的「诞生」发生在标量/容器的 end 时刻，值的「落户」（挂到父亲）发生在容器数据事件的 end 时刻**。键名由 `DICT_KEY` 的 end 记入当前帧，值由随后 `DICT_DATA` 的 end 入账，两者在框架栈的同一帧里配对。

#### 4.5.3 源码精读

**装配状态机的总线**：

- [src/ValueBaseStructParserStateMachine.cc:L81-L90](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ValueBaseStructParserStateMachine.cc#L81-L90) —— `beginElement` 分发给栈顶状态处理（状态自己决定压什么新状态）；`endElement` 分发给**新栈顶**处理后无条件 `pop()`——这就是「begin 压、end 弹」的机关所在。
- [src/ValueBaseStructParserStateMachine.cc:L45-L56](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ValueBaseStructParserStateMachine.cc#L45-L56) —— 九个状态对象是全局单例（匿名命名空间的裸 new，进程生存期），状态本身无字段，所有上下文都在状态机的 `sessionData_` 与控制器里——所以单例复用是安全的。

**会话数据桶**：

- [src/ValueBaseStructParserStateMachine.cc:L97-L114](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ValueBaseStructParserStateMachine.cc#L97-L114) —— 三个数据回调：字符**追加**进 `sessionData_.str`（多段字符的正确性由此保证）、数字三件套、布尔。
- [src/ValueBaseStructParserStateMachine.cc:L172-L203](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ValueBaseStructParserStateMachine.cc#L172-L203) —— `pushDictKeyState`/`pushStringState` 都先 `str.clear()`（新标量新桶）；`pushNumberState` 先清零。

**九个状态的行为**（全部分布在一个 143 行的文件里）：

- [src/ValueBaseStructParserStateImpl.cc:L44-L72](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ValueBaseStructParserStateImpl.cc#L44-L72) —— `valueState` 的 begin：遇 `DICT_T` 把 `Dict::g()` 设为当前帧值并压 `dictState`；遇 `ARRAY_T` 同理；遇标量只压对应状态（值晚点在 end 时诞生）。
- [src/ValueBaseStructParserStateImpl.cc:L74-L89](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ValueBaseStructParserStateImpl.cc#L74-L89) —— `dictState` 的 begin：`DICT_KEY_T` → `pushFrame()` + 压键状态；`DICT_DATA_T` → 压数据状态。
- [src/ValueBaseStructParserStateImpl.cc:L91-L95](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ValueBaseStructParserStateImpl.cc#L91-L95) —— 键的 end：`setCurrentFrameName(getCharacters())`，把 str 桶变成键名。
- [src/ValueBaseStructParserStateImpl.cc:L117-L140](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ValueBaseStructParserStateImpl.cc#L117-L140) —— 四种标量的 end：分别诞生 `String`、`Integer`（注释明言忽略 frac/exp）、`Bool`、`Null`。
- [src/ValueBaseStructParserStateImpl.cc:L97-L115](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ValueBaseStructParserStateImpl.cc#L97-L115) —— `DICT_DATA` 与 `ARRAY_DATA` 的 end 分别触发 `popDictFrame()` / `popArrayFrame()`——落户时刻。

**框架栈**（名字的由来与归属）：

- [src/XmlRpcRequestParserController.h:L51-L65](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/XmlRpcRequestParserController.h#L51-L65) —— `StateFrame` 持有「值 + 键名」；`validMember` 要求「有值且（允许空名或名字非空）」才算一个合法成员。
- [src/XmlRpcRequestParserController.cc:L43-L75](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/XmlRpcRequestParserController.cc#L43-L75) —— `pushFrame` 把当前帧入栈并开新帧；`popStructFrame` 弹出父帧（必为 Dict）、把当前帧的 (name, value) `put` 进去、父帧变当前帧；`popArrayFrame` 同理 `append`。控制器以 `XmlRpc` 命名是因为它最早为 XML-RPC 请求解析而写（u8-l4），后被 ValueBase 装配层复用——读代码时别被名字骗了。

**空键开关**：

- [src/ValueBaseBencodeParser.h:L46-L47](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ValueBaseBencodeParser.h#L46-L47) —— bencode 版产品把 `GenericParser` 第三模板参 `allowEmptyName` 设为 **true**：bencode 的键就是普通字符串，空串完全合法。
- [src/ValueBaseJsonParser.h:L46-L47](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ValueBaseJsonParser.h#L46-L47) —— JSON 版默认 **false**：`{"":"x"}` 是合法 JSON，但成员会被 `validMember` 判为非法而**静默丢弃**，最终得到空 Dict（测试 [test/ValueBaseJsonParserTest.cc:L148](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/ValueBaseJsonParserTest.cc#L148) 的 TODO 注释即此事）。

**GenericParser 粘合层**：

- [src/GenericParser.h:L45-L52](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/GenericParser.h#L45-L52) —— 组合两个成员：装配机 `psm_` 与以它为回调目标的词法机 `parser_`，构造时把 `allowEmptyName` 传给装配机。
- [src/GenericParser.h:L72-L84](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/GenericParser.h#L72-L84) —— `parseFinal(data, size, error&)`：错误时返回 `noResult()`（空指针），成功时 `getResult()` 取树，**无论成败最后都 `parser_.reset()`**——所以一个解析器对象可反复使用（RPC 服务里每个请求体都新建一个，见 [src/HttpServerBodyCommand.cc:L263](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerBodyCommand.cc#L263)）。
- [src/GenericParser.h:L91-L115](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/GenericParser.h#L91-L115) —— 附赠的 `parseFile` 帮手：4 KiB 一块读文件、逐块 `parseUpdate`、末尾 `parseFinal`，是「文件 → ValueBase」的一行式通道。

**真实调用方**（学以致用的地图）：

- [src/bencode2.cc:L63-L76](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bencode2.cc#L63-L76) —— `bencode2::decode`：解析失败抛 `BENCODE_PARSE_ERROR` 异常（[src/error_code.h:L71](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/error_code.h#L71)，错误码 25）。u6-l1 的 torrent 加载、u7-l2 的 DHT 报文工厂都是经它或直接用 `ValueBaseBencodeParser`（[src/bittorrent_helper.cc:L536](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L536)、[src/download_helper.cc:L255](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/download_helper.cc#L255)、[src/BtPostDownloadHandler.cc:L95](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtPostDownloadHandler.cc#L95)）。
- [src/HttpServerBodyCommand.cc:L263](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerBodyCommand.cc#L263) —— JSON-RPC 请求体一次性解析。
- [src/WebSocketSession.h:L110](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketSession.h#L110) 与 [src/WebSocketSession.cc:L321-L331](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/WebSocketSession.cc#L321-L331) —— 解析器作为会话成员常驻，WebSocket 每帧载荷增量 `parseUpdate`，全部帧收齐才 `parseFinal`——本讲开头说的「流式」在产品里的真实用法。

#### 4.5.4 代码实践

**实践目标**：验证「空键在两种格式下命运不同」，体会 `allowEmptyMemberName` 的作用。

**操作步骤**：

1. 跟踪 bencode 输入 `d0:1:xe`：键是长度 0 的空串，值是字符串 `x`。词法层完全正常发事件；装配层 `popStructFrame` 时 `validMember(true)`（bencode 产品为 true）通过，成员入账。预期得到 `Dict{ "" -> "x" }`，`size()==1`。
2. 跟踪 JSON 输入 `{"":"x"}`：词法层同样正常（键字符串合法）；装配层 `validMember(false)` 不通过（[src/XmlRpcRequestParserController.cc:L57-L59](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/XmlRpcRequestParserController.cc#L57-L59)），成员被丢弃。预期得到**空 Dict**，且 `error == 0`（不报错）。
3. 把上面两条写成断言（对照 [test/ValueBaseBencodeParserTest.cc:L88-L97](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/ValueBaseBencodeParserTest.cc#L88-L97) 的既有风格）追加进两个测试文件的 `testParseUpdate`，`make -C test check` 验证。

```cpp
// 示例代码：bencode 空键
std::string src = "d0:1:xe";
auto r = parser.parseFinal(src.c_str(), src.size(), error);
auto dict = downcast<Dict>(r);
CPPUNIT_ASSERT(dict);
CPPUNIT_ASSERT_EQUAL((size_t)1, dict->size());
CPPUNIT_ASSERT(dict->containsKey(""));
```

**需要观察的现象**：bencode 空键成员保留；JSON 空键成员被静默丢弃、解析器不报错。

**预期结果**：两条断言均通过；JSON 侧可加 `CPPUNIT_ASSERT(downcast<Dict>(r2)->empty());`。上机结果「待本地验证」。

#### 4.5.5 小练习与答案

**练习 1**：装配层的九个状态对象为什么可以全局单例？

**答案**：状态类没有成员变量（[src/ValueBaseStructParserStateImpl.h:L42-L162](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ValueBaseStructParserStateImpl.h#L42-L162) 里全是纯虚函数重载），一切可变数据都在状态机的 `sessionData_`、`stateStack_` 与控制器的框架栈里。无状态 → 可共享 → 免去每帧分配。

**练习 2**：`ValueBaseJsonParser` 解析失败后，同一个对象立即再用来解析合法输入，能成功吗？

**答案**：能。[src/GenericParser.h:L72-L84](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/GenericParser.h#L72-L84) 的 `parseFinal` 末尾无条件 `parser_.reset()`，它会连带 `psm_->reset()`（[src/JsonParser.cc:L529-L538](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/JsonParser.cc#L529-L538)），三本账全部归零。

**练习 3**：如果想让 aria2 的 JSON 解析支持「浮点数不截断」（生成一个新的 ValueBase 子类 `Double`），最少要动哪几处？

**答案**：这是一个很好的扩展性评估题。（1）`ValueBase.h/.cc` 增加 `Double` 类型与 visitor 重载；（2）`StructParserStateMachine.h` 的 `numberCallback` 已带 frac/exp 参数，无需改契约；（3）`NumberValueBaseStructParserState::endElement`（[src/ValueBaseStructParserStateImpl.cc:L123-L128](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ValueBaseStructParserStateImpl.cc#L123-L128)）改为生成 `Double`；（4）所有遍历 ValueBase 的 visitor（`json::encode`、`bencode2::encode`、RPC 序列化、DHT 编码……）都要补 `visit(const Double&)`。改动面主要在「消费者」而非解析器——这正是统一树表示的代价与收益。

## 5. 综合实践

**任务**：写一个完整的测试用例，用 `ValueBaseJsonParser` 解析一段嵌套 JSON，遍历并「打印」得到的 ValueBase 树，然后故意制造语法错误验证异常路径。这对应讲义规格里的实践任务，综合了本讲全部五个模块。

**第 1 步：写用例**。在 `test/JsonTest.cc` 中新增测试方法（需同时在 `test/JsonTest.h` 的 suite 宏里注册 `CPPUNIT_TEST(testMyValueBaseTree)`；若文件未包含 `<iostream>` 则补上）：

```cpp
// 示例代码：综合实践用例
void JsonTest::testMyValueBaseTree()
{
  json::ValueBaseJsonParser parser;
  ssize_t error;

  // 1. 嵌套 JSON：对象套数组套对象
  std::string src = "{\"name\":\"aria2\",\"loc\":80000,"
                    "\"files\":[\"aria2c\",\"aria2mon\"],"
                    "\"attrs\":{\"license\":\"GPL\",\"copyleft\":true}}";
  auto r = parser.parseFinal(src.c_str(), src.size(), error);

  // 2. 成功路径：error >= 0，拿到非空树
  CPPUNIT_ASSERT(r);
  CPPUNIT_ASSERT(error >= 0);

  // 3. 用 json::encode 把树重新序列化，等效于"打印整棵树"
  std::cout << "tree = " << json::encode(r.get()) << std::endl;

  // 4. 用 downcast 访问器逐层取值
  auto dict = downcast<Dict>(r);
  CPPUNIT_ASSERT(dict);
  CPPUNIT_ASSERT_EQUAL(std::string("aria2"),
                       downcast<String>(dict->get("name"))->s());
  CPPUNIT_ASSERT_EQUAL((Integer::ValueType)80000,
                       downcast<Integer>(dict->get("loc"))->i());
  auto files = downcast<List>(dict->get("files"));
  CPPUNIT_ASSERT_EQUAL((size_t)2, files->size());
  CPPUNIT_ASSERT_EQUAL(std::string("aria2mon"),
                       downcast<String>(files->get(1))->s());
  auto attrs = downcast<Dict>(dict->get("attrs"));
  CPPUNIT_ASSERT(downcast<Bool>(attrs->get("copyleft"))->val());

  // 5. 异常路径：对象缺值 -> ERR_UNEXPECTED_CHAR_BEFORE_VAL(-1)
  std::string bad = "{\"name\":}";
  auto r2 = parser.parseFinal(bad.c_str(), bad.size(), error);
  CPPUNIT_ASSERT(!r2);
  CPPUNIT_ASSERT_EQUAL((ssize_t)json::ERR_UNEXPECTED_CHAR_BEFORE_VAL, error);

  // 6. 异常路径：深度 51 -> ERR_STRUCTURE_TOO_DEEP(-11)
  std::string deep = std::string(51, '[') + std::string(51, ']');
  auto r3 = parser.parseFinal(deep.c_str(), deep.size(), error);
  CPPUNIT_ASSERT(!r3);
  CPPUNIT_ASSERT(error < 0);
}
```

**第 2 步：编译运行**。在项目根目录执行（前置：已按 u1-l2 完成 `./configure`）：

```bash
make -C src          # 重新编译 libaria2（若 test 依赖已变动）
make -C test check   # 构建并运行测试套件（test/aria2c）
```

测试套件会跑全部用例；`std::cout` 的树打印会直接出现在终端输出里。

**第 3 步：对照源码解释现象**。回答三个问题：

1. 为什么第 5 步错误码是 -1 而不是 -3（`ERR_UNEXPECTED_CHAR_BEFORE_OBJ_VAL`）？提示：`:` 之后已经发出了 `begin(DICT_DATA)`，解析器已切换到 `VALUE` 状态，`}` 落在 [src/JsonParser.cc:L129-L178](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/JsonParser.cc#L129-L178) 的 `VALUE` 分派里。
2. 第 6 步的错误码是多少？提示：[src/JsonParser.cc:L602-L611](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/JsonParser.cc#L602-L611) 的限深检查在压第 51 层时报出。
3. 把 `deep` 改成 50 层会怎样？提示：[test/ValueBaseJsonParserTest.cc:L280-L282](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/ValueBaseJsonParserTest.cc#L280-L282) 同时测了 50 层成功（外层是 50 个 `[` 但内含一个对象，总深度恰好压线）与 51 层失败两种情况。

**预期结果**：三条断言块全部通过；控制台打印出的 `tree =` 行是一段键按字典序排列的 JSON（`attrs, files, loc, name`）——正好复验 4.1 的有序 Dict 结论。若本机无法编译，练习的等效替代是：手写 `src` 的 ValueBase 树与事件序列各一份，再逐条对照 [test/ValueBaseJsonParserTest.cc:L26-L213](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/ValueBaseJsonParserTest.cc#L26-L213) 的既有断言自查。

## 6. 本讲小结

- **ValueBase 是 aria2 的动态类型**：六类型（String/Integer/Bool/Null/List/Dict）、`unique_ptr` 树形所有权、`std::map` 带来的字典序确定性（infohash 重编码不变的基础）、`downcast<T>()` 提供类型安全访问、`Dict::put` 覆盖重复键。
- **两层拆分是骨架的精髓**：词法机（BencodeParser/JsonParser）只认字节、发事件；装配机（ValueBaseStructParserStateMachine）只听事件、建树。两者之间的契约就是 `StructParserStateMachine` 的九种结构事件与五个回调。
- **两台词法机同构**：`parseUpdate/parseFinal/reset` 三件套、`currentState_ + stateStack_` 双栈、`onValueEnd` 栈驱动归位、50 层深度上限、负数错误码即返回值；差异全部来自字法（长度前缀 vs 引号转义、无标点 vs `:,`[]` 标点、无浮点 vs 三段数字与 `\u` 转码）。
- **装配层靠三本账**：装配状态栈（begin 压/end 弹）、会话数据桶（str 追加、标量 end 诞生）、框架栈（`XmlRpcRequestParserController`，历史命名，实为通用树账本；`validMember` 决定空键成员去留）。
- **GenericParser 焊出产品**：`ValueBaseBencodeParser`（允许空键）与 `ValueBaseJsonParser`（禁止空键）各一行 typedef；`parseFinal` 失败给空指针、成功给树、收尾必 reset；`bencode2::decode` 是带异常的门面。
- **流式是设计的第一约束**：进度全部外置，任意切割的字节流都能正确解析；WebSocket 会话逐帧 `parseUpdate` 是其在产品中的活样本。

## 7. 下一步学习建议

- **u9-l5（测试体系）**：本讲多处实践依赖「往 test/ 加断言 + make check」，下一讲系统讲 CppUnit 组织与单个用例的运行方法，正好补齐工具链。
- **回看消费者**：带着「树建好了然后呢」的问题重读 [src/DHTMessageFactoryImpl.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DHTMessageFactoryImpl.cc)（u7-l2）与 [src/HttpServerBodyCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerBodyCommand.cc)（u8-l3/u8-l4），观察 `Dict::get` + `downcast` 的判空流水线如何替代 schema 校验。
- **对照另一套解析骨架**：[src/XmlRpcRequestParserStateMachine.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/XmlRpcRequestParserStateMachine.cc) 是 SAX 风格的 XML-RPC 解析（u8-l1 的 Metalink 同族），对比它与本讲「词法/装配分层」的异同，理解同一作者在同一代码库里对「结构化解析」的两种组织方式。
- **动手方向**：若想检验掌握程度，可尝试 4.5.5 练习 3 的 `Double` 扩展草案（只写设计文档不改代码），或给 4.2 的事件打印机补上「彩色缩进打印树」的能力，作为 u9-l6 二次开发实战的热身。
