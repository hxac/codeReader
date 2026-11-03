# 命令行选项系统：解析、校验与帮助文本

## 1. 本讲目标

学完本讲，你应该能够：

1. 跟踪一个命令行参数（例如 `--max-connection-per-server=4`）从 `argv` 字符串一路走到 `Option` 对象内部存储的完整路径。
2. 解释 aria2 选项值的四层优先级：**内置默认值 < 配置文件 < 环境变量 < 命令行**，并知道这个顺序是由哪段代码保证的。
3. 理解 `OptionHandler` 家族如何完成类型转换与取值校验（为什么 `=99` 会被拒绝、报错文本从哪一行代码产生）。
4. 学会从 `prefs.cc` / `OptionHandlerFactory.cc` / `usage_text.h` 三个文件中查出任意一个 `--option` 的定义、类型、默认值、取值范围和帮助文本。
5. 明白 `aria2c -h`、`aria2c -h#http`、`aria2c --help=split` 这些帮助形式分别是怎么生成出来的。

本讲是第一单元（入门篇）的最后一讲。前四讲解决了「项目是什么、怎么编译、从哪进入、目录怎么读」，本讲解决「aria2 那两百多个 `--option` 是如何被组织、解析和校验的」——这是后面所有讲义的基础，因为几乎每个子系统（限速、BT、RPC……）的行为都由 `Option` 里的键值对驱动。

## 2. 前置知识

本讲不需要你已经读过 aria2 的其他代码，但以下概念会帮你更快理解：

- **`argc`/`argv`**：C/C++ 程序入口 `main(int argc, char** argv)` 收到的原始命令行参数。`argc` 是参数个数，`argv` 是每个参数的字符串，`argv[0]` 是程序名。
- **`getopt_long`**：C 库（POSIX + GNU 扩展）提供的命令行解析函数，负责识别 `--long-option=value`、`--long-option value`、`-x value` 这些写法，把「选项部分」和「非选项参数（比如 URI）」分开。aria2 没有自己造轮子，而是包装了它。
- **键值存储（字典）**：把「名字 → 值」成对保存的数据结构。aria2 的 `Option` 类本质上是一个把键编号当作数组下标使用的特殊字典。
- **位图（bitfield）**：用一个字节的 8 个比特分别表示 8 个事物的开/关状态，节省内存。`Option` 用它记录「哪些键被赋过值」。
- **工厂模式与模板方法模式**：工厂模式指「由一个函数统一创建一族对象」（本讲中 `OptionHandlerFactory` 创建各种 `OptionHandler`）；模板方法模式指「基类固定算法骨架，子类只填充其中一步」（本讲中 `AbstractOptionHandler::parse` 固定 try/catch 骨架，子类实现 `parseArg`）。
- **int64 取值范围**：`NumberOptionHandler` 用 `int64_t`（64 位整数）存数字选项，`min_`/`max_` 为 `-1` 时表示该侧不设限。

另外回顾上一讲（u1-l3）的结论：`Context` 构造函数的第一个阶段就是「选项解析」，本讲正是把那一阶段展开讲透。

## 3. 本讲源码地图

| 文件 | 行数 | 作用 |
| --- | --- | --- |
| [src/option_processing.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/option_processing.cc#L174-L345) | 347 | 选项处理总指挥：先探路 `--no-conf`/`--conf-path`，再按「默认值→配置文件→环境变量→命令行」的顺序合并选项，并处理所有错误分支 |
| [src/OptionParser.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionParser.cc#L155-L235) | 377 | 把 `argv` 交给 `getopt_long` 拆解，产出 `NAME=VALUE` 文本流；也提供按标签/按名字查找 handler 的接口 |
| [src/OptionParser.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionParser.h#L54-L104) | 108 | `OptionParser` 类声明，注释里写清了各方法契约 |
| [src/Option.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Option.cc#L44-L115) | 196 | 选项值的存储：数组 + 位图 + 父链（parent）三层结构 |
| [src/Option.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Option.h#L48-L97) | 102 | `Option` 类声明，注释详细解释了 `defined`/`blank`/`get` 的语义 |
| [src/prefs.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/prefs.cc#L47-L112) | 595 | 所有选项键的「户口登记处」：`PrefFactory` 给每个键发编号；文件后半部分是键常量的定义 |
| [src/prefs.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/prefs.h#L43-L69) | 400+ | `Pref` 结构体声明、`option::countOption/i2p/k2p` 接口，以及全部 `PREF_*` 键常量 |
| [src/OptionHandler.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandler.h#L61-L109) | 118 | `OptionHandler` 纯虚接口：一个选项一个处理器 |
| [src/AbstractOptionHandler.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractOptionHandler.cc#L47-L109) | 171 | 基类实现：统一的 `parse` 骨架（try/catch 换异常）+ 标签/标志位管理 |
| [src/OptionHandlerImpl.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerImpl.h#L50-L200) | 240+ | 具体处理器家族：`BooleanOptionHandler`、`NumberOptionHandler`、`DefaultOptionHandler` 等 |
| [src/OptionHandlerImpl.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerImpl.cc#L138-L185) | 400+ | 处理器的校验逻辑，包括数字范围检查与报错文本 |
| [src/OptionHandlerFactory.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc#L48-L1997) | 1999 | 一个约 1950 行的大工厂函数：为每个选项 new 出处理器、打标签、设标志 |
| [src/usage_text.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/usage_text.h#L38-L44) | 1133 | 帮助文本宏定义：每个选项的说明文字是一个 `TEXT_*` 宏 |
| [src/OptionHandler.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandler.cc#L67-L78) | 81 | 把一个 handler 格式化打印（说明/可取值/默认值/标签） |
| [src/version_usage.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/version_usage.cc#L84-L183) | 186 | `showUsage`/`showVersion`：`-h` 与 `--version` 输出的组装现场 |
| [src/help_tags.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/help_tags.h#L42-L61) | 73 | 帮助标签枚举（`#basic`、`#http`……） |

一条主线先记住：**键的定义在 `prefs.cc`，键的「语法」在 `OptionHandlerFactory.cc`，键的帮助文本在 `usage_text.h`，值的存储在 `Option.cc`，值的入口在 `option_processing.cc`**。想查任何一个选项，按这五个文件顺序检索即可。

## 4. 核心概念与源码讲解

### 4.1 prefs 键表与 Option 存储：选项的身份证与仓库

#### 4.1.1 概念说明

aria2 有两百多个选项（`--split`、`--max-connection-per-server`、`--dir`……）。最朴素的实现是用 `std::map<std::string, std::string>` 存「选项名 → 值」，但 aria2 选择了更高效的方式：

1. **每个选项名登记一次，换取一个整数编号（ID）**——这就是 `Pref`（preference 的缩写）。名字字符串只存这一份，之后全程序用指针 `PrefPtr` 传递。
2. **`Option` 用「以 ID 为下标的数组」存值**，查一个值就是一次数组访问，O(1) 且无字符串比较。
3. **一张位图记录哪些 ID 被赋过值**，另加一条**父链（parent）**让多个 `Option` 对象分层共享——命令行选项对象的父链指向配置文件选项对象，查询时「本地没有就问父亲」，优先级由此自然实现。

这个设计解决了两个问题：性能（高频读取的选项不会反复做哈希/比较）和分层覆盖（命令行覆盖配置文件不需要拷贝整张表）。

#### 4.1.2 核心流程

```text
程序启动
  └─ PrefFactory（单例）依次执行 makePref("timeout")、makePref("dir")……
        ├─ nextId(): 发放递增整数 ID（0 号留给特殊的"空键"）
        ├─ i2p_ 数组: ID → Pref*（反查）
        └─ k2p_  map : 名字 → Pref*（正查）
  └─ 之后任何代码:
        option::k2p("split")     → PrefPtr（名字查键，查不到返回 ID 为 0 的空键）
        option::i2p(37)          → PrefPtr（ID 查键）
        option::countOption()    → 已登记的键总数
```

`Option` 侧的读写规则：

```text
put(pref, v)     : 置位图中该 ID 的比特，table_[ID] = v
get(pref)        : 本地比特已置 → 返回 table_[ID]
                   否则若 parent 存在 → 递归问 parent
                   否则返回空字符串
defined(pref)    : 本地置位 或 parent 链上任一层置位
getAsInt/getAsBool/getAsDouble : get() 之后用 strtol/strtod 等转类型
```

#### 4.1.3 源码精读

键的登记处——`PrefFactory`，注意构造函数里第 0 号是特殊的空键，`k2p` 查不到时返回它而不是 `nullptr`：

[src/prefs.cc:47-89](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/prefs.cc#L47-L89) —— `PrefFactory` 用 `makePref` 给每个选项名分配递增 ID，同时维护 `i2p_`（ID→键）与 `k2p_`（名字→键）两张索引；`k2p` 对未知名字返回 ID 为 0 的空键。这是一个懒加载的单例（首次调用 `makePref` 时构造）。

[src/prefs.cc:102-112](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/prefs.cc#L102-L112) —— 对外的四个自由函数 `countOption()`/`i2p()`/`k2p()`/`deletePrefResource()`，全程序都通过 `option::` 命名空间使用键表。

[src/prefs.cc:316](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/prefs.cc#L316) —— 本讲的主角之一在此登记：`PREF_MAX_CONNECTION_PER_SERVER = makePref("max-connection-per-server")`。`prefs.cc` 中共有约 220 行这样的登记（`grep -c "makePref(" src/prefs.cc` 结果为 221，含 1 个空键）。每个键上方还有一行注释说明合法取值形态，例如 [src/prefs.cc:311-312](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/prefs.cc#L311-L312) 的 `// value: 1*digit`。

[src/prefs.h:43-51](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/prefs.h#L43-L51) —— `Pref` 结构体只有两个字段：`k`（关键字，即选项名）和 `i`（整数 ID）；`PrefPtr` 就是 `const Pref*`。此外 [src/prefs.h:74-76](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/prefs.h#L74-L76) 定义了 `A2_V_TRUE("true")`/`A2_V_FALSE("false")` 等常用常量——布尔选项存的也是字符串。

值的仓库——`Option`：

[src/Option.cc:44-47](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Option.cc#L44-L47) —— 构造函数按键总数分配 `table_`（值数组）和 `use_`（位图），这就是「ID 当下标」的落地。

[src/Option.cc:77-87](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Option.cc#L77-L87) —— `put` 置位并写值；`defined` 先查本地位图，再沿 `parent_` 递归。注意位运算是 `128 >> (pref->i % 8)`，即高位在前的位序。

[src/Option.cc:104-115](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Option.cc#L104-L115) —— `get` 的三级查找：本地 → 父链 → 空字符串。**命令行能覆盖配置文件的秘密就在这**：命令行 `Option` 的 parent 是配置文件 `Option`，本地没写过的键自动「继承」父层的值。

[src/Option.cc:117-139](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Option.cc#L117-L139) —— 取值时的类型转换：`getAsInt` 用 `strtol`、`getAsLLInt` 用 `strtoll`、`getAsBool` 是与 `"true"` 字符串比较、`getAsDouble` 用 `strtod`。也就是说 **`Option` 内部一切皆字符串，类型转换发生在读取时**（而入口处的 `OptionHandler` 已经保证了字符串格式合法）。

[src/Option.cc:183-186](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Option.cc#L183-L186) —— `setParent` 建立分层链，这个调用发生在 `option_processing` 的收尾处（见 4.4 节）。

#### 4.1.4 代码实践

1. **实践目标**：亲手数一遍键表规模，并确认「未知键返回空键」这一行为。
2. **操作步骤**：
   - 在项目根目录执行 `grep -c "makePref(" src/prefs.cc`，得到登记总数（应为 221，其中 1 个是空键）。
   - 再执行 `grep -n "PREF_SPLIT\|PREF_DIR " src/prefs.cc`，找到 `--split` 和 `--dir` 对应的键定义行，抄下它们的注释（合法取值形态）。
   - 阅读 [src/prefs.cc:76-83](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/prefs.cc#L76-L83)，确认 `k2p` 对查不到的名字返回 `i2p_[0]`（空键）。
3. **需要观察的现象**：`prefs.cc` 的登记行数（约 220）与 `OptionHandlerFactory.cc` 中 `handlers.push_back` 的次数（约 207）并不相等。
4. **预期结果**：你会得出结论——**少数键只供程序内部使用，没有对应的命令行处理器**（例如运行时才写入的状态键）；「能用 `--xxx` 设置的选项」是键表的一个子集。（待本地验证具体差集，可用两份 grep 结果做 diff。）
5. 本实践为纯源码阅读型，无需编译。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Option::get` 查不到时返回 `A2STR::NIL`（空字符串）而不是抛异常？

**答案**：因为「未设置」在 aria2 里是正常状态而非错误——调用方通常先 `put` 过默认值（`parseDefaultValues` 会把所有有默认值的键写入配置层 `Option`），或者用 `defined`/`blank` 先判断。让 `get` 永远安全返回，可以让大量「读取配置」的调用点免于异常处理；真正的非法值在入口（`OptionHandler::parse`）就被拦截了，存储层不会再遇到坏数据。

**练习 2**：`Option::getAsBool` 的判断是 `get(pref) == A2_V_TRUE`。如果我直接用 `option->put(PREF_XXX, "yes")` 绕过处理器写入，`getAsBool` 会返回什么？

**答案**：返回 `false`。因为布尔处理器 `BooleanOptionHandler` 只接受 `"true"`/`"false"`（见 4.2 节），`"yes"` 会被入口拒绝；一旦绕过入口直接写入，`getAsBool` 只认字符串 `"true"`，`"yes"` 会被当成假。这解释了为什么 aria2 文档中布尔选项只写 `true|false` 而不支持 `yes/no`。

**练习 3**：`Option` 的拷贝构造函数（[src/Option.cc:51](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Option.cc#L51)）连 `parent_` 一起拷贝。这样拷贝出来的对象修改自己的键，会影响原对象吗？

**答案**：不会影响原对象的本地表（`table_`/`use_` 是值拷贝），但两者共享同一个 parent。也就是说修改拷贝对象的某个键只遮蔽（shadow）原对象的同名键；而通过拷贝对象去 `remove(pref)` 时会级联删除 parent 里的键（[src/Option.cc:158-164](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Option.cc#L158-L164)），这才是会影响共享层的操作。

### 4.2 OptionHandler 体系：一个选项一个校验器

#### 4.2.1 概念说明

「选项名」和「值的合法性规则」是两回事。`prefs.cc` 只登记名字，而**每个选项还配有一个 `OptionHandler` 对象**，负责三件事：

1. **校验并转换**：检查传入字符串是否合法（数字是否在范围内、布尔是否为 true/false、参数是否在枚举集合中），合法则以规范化字符串写入 `Option`。
2. **携带元数据**：帮助文本、默认值、短选项名、参数类型（必须带值/可选值/无值）、标签（属于 `#basic` 还是 `#http`……）、各种标志位（是否隐藏、RPC 能否运行时修改等）。
3. **生成帮助**：`createPossibleValuesString()` 产出「可取值」一行（例如 `1-16`）。

不同类型对应不同子类：`BooleanOptionHandler`（true/false）、`NumberOptionHandler`（带 min/max 的 64 位整数）、`UnitNumberOptionHandler`（支持 `1M`/`2G` 单位的数字）、`FloatNumberOptionHandler`、`IntegerRangeOptionHandler`、`DefaultOptionHandler`（不限类型）、`ParameterOptionHandler`（枚举）、`HostPortOptionHandler`/`HttpProxyOptionHandler`（一个选项拆写成多个键）等。

#### 4.2.2 核心流程

一个值进入存储的固定骨架（模板方法模式）：

```text
调用方: handler->parse(option, "99")
  │
  ├─ AbstractOptionHandler::parse        （固定骨架）
  │    try  { parseArg(option, "99"); }  ← 子类实现，唯一可变的一步
  │    catch(Exception& e) {
  │        换成 OptionHandlerException 抛出（附上 pref 与错误码 OPTION_ERROR）
  │    }
  │
  └─ NumberOptionHandler::parseArg
       ├─ util::parseLLIntNoThrow 解析失败 → 抛 "Bad number"
       └─ 成功 → 检查 min_ <= n <= max_
             ├─ 通过 → option.put(pref, itos(n))
             └─ 不通过 → 抛 "xxx must be between 1 and 16."
```

选项的注册流程：

```text
OptionParser::getInstance()                       （首次调用）
  └─ OptionHandlerFactory::createOptionHandlers()  （约 1950 行大函数）
        对每个选项:
          new 一个具体 Handler(键, TEXT_帮助宏, 默认值, min, max, 短名)
          addTag(...) 若干次
          setInitialOption / setChangeGlobalOption / ...
          handlers.push_back(op)
  └─ OptionParser::setOptionHandlers(handlers)
        handlers_[pref->i] = handler        （按 ID 下标直接放）
        有短名的再登记进 shortOpts_[256]
```

#### 4.2.3 源码精读

[src/OptionHandler.h:61-109](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandler.h#L61-L109) —— 纯虚接口 `OptionHandler`。注意 [L87](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandler.h#L87) 的枚举 `ARG_TYPE { REQ_ARG, OPT_ARG, NO_ARG }`，分别对应「必须带值」「值可省略（如 `--daemon[=true|false]`）」「无值（如 `--version`）」。

[src/AbstractOptionHandler.cc:63-71](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractOptionHandler.cc#L63-L71) —— 统一的 `parse` 骨架：把子类 `parseArg` 抛出的任意 `Exception` 统一包装成 `OptionHandlerException`（错误码 `OPTION_ERROR = 28`，见 [src/error_code.h:74](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/error_code.h#L74)），并附上出错的 `pref`，这样最外层就能精准打印该选项的用法。

[src/OptionHandlerImpl.cc:151-185](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerImpl.cc#L151-L185) —— `NumberOptionHandler` 的核心：先做数字解析，再做范围检查；**第 176 行的 `fmt(_("must be between %" PRId64 " and %" PRId64 "."), min_, max_)` 就是 `--max-connection-per-server=99` 报错文本的直接来源**（拼接上键名后为 `max-connection-per-server must be between 1 and 16.`）。`min_`/`max_` 为 `-1` 表示该侧不限制，此时报错措辞会换成「must be greater than …」或「must be smaller than …」。

[src/OptionHandlerImpl.cc:80-97](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerImpl.cc#L80-L97) —— `BooleanOptionHandler::parseArg`：只认 `"true"`/`"false"`；若参数类型是 OPT_ARG/NO_ARG，则空串也当 `true`（这就是 `--daemon` 不写值也生效的原因）。

[src/OptionHandlerImpl.h:77-104](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerImpl.h#L77-L104) —— `NumberOptionHandler` 与其子类 `UnitNumberOptionHandler`（允许 `K/M` 后缀，用于 `--min-split-size` 这类选项）的声明；同文件还有 `FloatNumberOptionHandler`（[L106-L121](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerImpl.h#L106-L121)）、`DefaultOptionHandler`（[L123-L139](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerImpl.h#L123-L139)）、`CumulativeOptionHandler`（[L141-L157](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerImpl.h#L141-L157)，可重复出现、用分隔符拼接，如 `--header`）、`ParameterOptionHandler`（[L187-L200](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerImpl.h#L187-L200)，枚举校验）等。

[src/OptionHandlerFactory.cc:438-449](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc#L438-L449) —— 本讲的样板注册块：`--max-connection-per-server` 被注册为 `NumberOptionHandler`，**默认值 `"1"`，范围 `1..16`，短选项 `'x'`**，打了 `TAG_BASIC`/`TAG_FTP`/`TAG_HTTP` 三个标签，并设置 `setInitialOption`（可在创建任务时按任务覆盖）与 `setChangeGlobalOption`/`setChangeOptionForReserved`（允许 RPC `changeGlobalOption` 运行时修改）。整个工厂共约 207 个这样的块。

[src/usage_text.h:742-744](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/usage_text.h#L742-L744) —— 上一个块中第二个参数 `TEXT_MAX_CONNECTION_PER_SERVER` 的定义处：帮助文本是一段预排版的字符串宏（` -x, --max-connection-per-server=NUM …`），工厂注册时把它「注入」处理器。这就是「帮助文本与注册逻辑分离」的组织方式。

[src/OptionParser.cc:365-373](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionParser.cc#L365-L373) —— `OptionParser::getInstance()` 懒加载单例，首次调用才触发工厂创建全部处理器——所以这套注册的开销只在第一次用到选项系统时发生。

[src/OptionParser.cc:281-289](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionParser.cc#L281-L289) —— `addOptionHandler`：`handlers_[pref->i] = handler` 直接按下标存放（数组在构造函数里按 `countOption()` 预分配，见 [src/OptionParser.cc:60-63](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionParser.cc#L60-L63)）；短名则登记进 256 长度的 `shortOpts_` 表。

#### 4.2.4 代码实践（本讲主实践）

1. **实践目标**：亲手查出一个选项的全部「档案」，并验证非法值报错的完整链路。
2. **操作步骤**：
   - 打开 [src/OptionHandlerFactory.cc:438-449](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc#L438-L449)，抄录 `--max-connection-per-server` 的注册信息。**答案先行**：类型 `NumberOptionHandler`（64 位整数），默认值 `1`，取值范围 `1-16`，短选项 `-x`，标签 `#basic,#ftp,#http`。
   - 如果你已完成 u1-l2 的编译，运行 `./src/aria2c --max-connection-per-server=99 https://example.com`；若尚未编译，此步标注「待本地验证」。
   - 紧接着运行 `echo $?` 记录进程退出码。
   - 对照源码解释报错（见下方「预期结果」的链路）。
   - 再试 `./src/aria2c -x99 …`（短选项形式）与 `./src/aria2c --max-connection-per-server=16 …`（合法上界），确认行为一致/不同。
3. **需要观察的现象**：终端先打印一段 `Exception: […] errorCode=…` 栈回溯，其中提到 `OptionHandlerImpl.cc` 的行号；随后打印 `Usage:` 与 `-x, --max-connection-per-server=NUM …` 的说明、`Possible Values: 1-16`、`Default: 1`、`Tags: basic, ftp, http`。
4. **预期结果**：退出码为 **28**（`OPTION_ERROR`）。报错链路共五步：
   - `getopt_long` 正常识别选项，`optarg = "99"`，被写进 `NAME=VALUE` 流水（见 4.3 节）；
   - [src/OptionHandlerImpl.cc:165-184](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerImpl.cc#L165-L184) 的范围检查失败，抛出 `DlAbortEx("max-connection-per-server must be between 1 and 16.")`；
   - [src/AbstractOptionHandler.cc:63-71](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractOptionHandler.cc#L63-L71) 把它包成 `OptionHandlerException`（错误码定为 `OPTION_ERROR`，消息为 "We encountered a problem while processing the option '--max-connection-per-server'."，见 [src/OptionHandlerException.cc:53-60](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerException.cc#L53-L60)）；
   - [src/option_processing.cc:283-292](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/option_processing.cc#L283-L292) 捕获后打印异常链和该选项的 Usage；
   - 返回错误码 28，`Context` 构造函数（[src/Context.cc:158-162](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Context.cc#L158-L162)）在 standalone 模式直接 `exit(28)`。
5. 若无法本地编译运行，第 2 步起标注「待本地验证」，但第 1 步的档案抄录与链路推理可纯读码完成。

#### 4.2.5 小练习与答案

**练习 1**：`--dir` 这类路径选项应该用哪个处理器？如果它用 `NumberOptionHandler` 会发生什么？

**答案**：查 [src/OptionHandlerFactory.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc#L48-L1997) 可知 `PREF_DIR` 用 `DefaultOptionHandler`（不限类型，仅作字符串存储）。若错用 `NumberOptionHandler`，任何非数字路径（如 `/tmp`）都会在 `parseLLIntNoThrow` 处抛 "Bad number"，选项彻底不可用。选对处理器就是给选项定「类型」。

**练习 2**：为什么 `--max-connection-per-server` 要同时打 `TAG_BASIC`、`TAG_FTP`、`TAG_HTTP` 三个标签？

**答案**：标签决定该选项出现在哪些帮助分类里（见 4.5 节）。这个选项同时影响 HTTP 和 FTP 下载，又是新手必须了解的基础选项，所以三个标签都打——用户执行 `aria2c -h`（默认 `#basic`）、`aria2c -h#http`、`aria2c -h#ftp` 时都能看到它。标签存放在 [src/AbstractOptionHandler.cc:73-78](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractOptionHandler.cc#L73-L78) 的位标志 `tags_` 中，一个 32 位整数最多容纳 32 个标签。

**练习 3**：`setEraseAfterParse(true)` 标志是干什么的？哪个选项最需要它？

**答案**：见 [src/OptionHandler.h:93-95](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandler.h#L93-L95) 的注释：把 `argv` 中的选项值原地覆写成 `*`，避免敏感值出现在 `ps` 的进程列表里。最需要的是带密码的选项，例如 `--rpc-secret`、`--ftp-password`。覆写动作发生在 [src/OptionParser.cc:226-230](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionParser.cc#L226-L230)。

### 4.3 OptionParser：从 argv 到 NAME=VALUE 流水

#### 4.3.1 概念说明

`OptionParser` 是命令行与处理器之间的「翻译官」。它有一个非常巧妙的中间层设计：**不直接把解析结果写进 `Option` 对象，而是先输出成 `NAME=VALUE` 文本流**。这样做的好处是一石三鸟：

- **配置文件复用**：`aria2.conf` 的每一行本来就是 `name=value` 格式，与命令行解析产物同构，同一条 `parse(Option&, istream&)` 通道就能吃下两种来源；
- **libaria2 复用**：嵌入模式的调用方传来的 `KeyVals`（键值对数组）走 `parse(Option&, const KeyVals&)`，同样汇入处理器；
- **两遍解析**：同一份命令行文本流可以被重放（rewind）多次——先探路读 `--no-conf`，再正式解析全部（见 4.4 节）。

#### 4.3.2 核心流程

`parseArg`（命令行 → 文本流）：

```text
1. 遍历全部 handler，统计未隐藏的选项数
2. putOptions: 把每个 handler 翻译成一个 struct option（getopt_long 的长选项表）
     REQ_ARG → required_argument / OPT_ARG → optional_argument / NO_ARG → no_argument
     无短名: flag=&lopt, val=pref->i（返回时用 ID 定位 handler）
     有短名: val=短字符（返回时走 findByShortName）
3. createOptstring: 拼出短选项串（如 "x:" 表示 -x 需要参数）
4. 循环 getopt_long:
     c == -1  → 结束
     c == 0   → 长选项，用 lopt 里的 ID 找 handler
     c == '?' → 出错：区分"完全未知的选项"与"缩写歧义/缺参数"，前者抛 UnknownOptionException
     其他     → 短选项字符找 handler
     输出 "name=value\n" 到流（可按需把 value 覆写为 '*'）
5. argv 剩余部分（optind 之后）全部收进 nonopts —— 这就是 URI 列表
```

`parse`（文本流 → Option）：

```text
逐行读:
  空行或以 # 开头 → 跳过（配置文件注释）
  按 '=' 切分成 name/value（util::divide）
  k2p(name) 查键 → find(pref) 查 handler
    有 handler → handler->parse(option, value)   ← 走 4.2 的校验链
    无 handler → 只打 WARN 日志，不报错！
```

#### 4.3.3 源码精读

[src/OptionParser.cc:155-166](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionParser.cc#L155-L166) —— `parseArg` 开头：统计公开选项数、构建 `struct option` 数组与短选项串，然后进入 `getopt_long` 循环。

[src/OptionParser.cc:86-130](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionParser.cc#L86-L130) —— `putOptions` 的翻译规则：`ARG_TYPE` 映射到 `getopt_long` 的三种参数要求；注意无短名选项用 `flag=&lopt, val=pref->i` 的技巧让 `getopt_long` 把选项 ID 写进 `lopt`，`c == 0` 时据此定位 handler（[src/OptionParser.cc:171-173](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionParser.cc#L171-L173)）。

[src/OptionParser.cc:178-221](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionParser.cc#L178-L221) —— `c == '?'` 的精细分诊：注释写明三种情形（完全未知 / 参数缺失 / 缩写歧义）。代码逐个比对已知选项名：精确命中说明是别处出错；恰好只有一个选项以输入为前缀（`ambiguous == 1`）说明缩写本来合法、错误另有所在；否则抛 `UNKNOWN_OPTION_EXCEPTION(argv[optind - 1])`。**这也告诉我们 GNU `getopt_long` 原生支持无歧义的长选项缩写**，例如 `--max-conn` 能被识别为 `--max-connection-per-server`（它不是 `--max-concurrent-downloads` 的前缀，因为第 8 个字符 `n` vs `c` 不同）。

[src/OptionParser.cc:222-234](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionParser.cc#L222-L234) —— 每识别一个选项就输出 `name=value` 行（值可被覆写成 `*` 防泄露），最后 `argv + optind` 之后的非选项参数全部收进 `nonopts`——这就是传给 `MultiUrlRequestInfo` 的 URI 列表的最初来源。

[src/OptionParser.cc:237-257](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionParser.cc#L237-L257) —— 文本流版本 `parse`：`#` 注释、`=` 切分、`k2p` 查键、交给 handler 校验写入；**未知键只打 WARN 日志不报错**（第 254 行）。这与命令行的处理（直接抛异常退出）形成对比——配置文件更宽容，因为版本升级后旧配置文件里可能残留已改名的选项。

[src/OptionParser.cc:259-271](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionParser.cc#L259-L271) —— `KeyVals` 版本 `parse`，libaria2 嵌入模式（`aria2::addUri` 的 options 参数）从这进入，逻辑与文本流版本一致。

[src/OptionParser.cc:291-298](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionParser.cc#L291-L298) —— `parseDefaultValues`：遍历所有有默认值的 handler，把默认值「当作用户输入」走一遍校验并写入。这保证默认值本身也经过同一套校验逻辑。

[src/OptionParser.cc:300-361](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionParser.cc#L300-L361) —— 查询家族：`findByTag`（帮助分类）、`findByNameSubstring`（`--help=split` 的模糊匹配）、`findAll`（`#all`）、`find/findById`（注意 `handlers_[0]` 被当作「空处理器」兜底返回，与键表的空键 ID 0 呼应）、`findByShortName`。这些查询是帮助系统（4.5 节）的数据来源。

#### 4.3.4 代码实践

1. **实践目标**：验证长选项缩写机制，加深对 `parseArg` 分诊逻辑的理解。
2. **操作步骤**：
   - 纯读码：在 [src/OptionParser.cc:198-219](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionParser.cc#L198-L219) 中找到「精确命中」「唯一前缀命中（ambiguous == 1）」「抛 UNKNOWN_OPTION_EXCEPTION」三个分支各自的条件。
   - 若已编译：运行 `./src/aria2c --max-conn=4 --dry-run …` 或 `./src/aria2c --max-conn 4 https://example.com`，观察是否被接受；再运行 `./src/aria2c --max-c=4 …`，观察是否因歧义（同时匹配 `--max-connection-per-server`、`--max-concurrent-downloads`、`--max-connection-limit` 等多个选项）而报错（待本地验证；`--max-c` 是否歧义取决于当时所有以 `max-c` 开头的选项集合）。
3. **需要观察的现象**：无歧义缩写能正常工作；歧义缩写触发 `UnknownOptionException`，且错误输出末尾会出现「Did you mean:」候选列表（该列表来自 4.4 节的 `showCandidates`）。
4. **预期结果**：与源码推理一致——`getopt_long` 对无歧义前缀静默接受，aria2 只在 `?` 分支做补充诊断。若无法运行，标注「待本地验证」。
5. 附加读码任务：解释为什么 `parseArg` 输出到 `std::ostream` 而不是直接写 `Option`。答案见 4.3.1 概念说明（配置文件复用 + 两遍解析）。

#### 4.3.5 小练习与答案

**练习 1**：`aria2c -x16 URL` 和 `aria2c -x 16 URL` 都能工作吗？依据是什么？

**答案**：都能。`-x` 的注册中短名为 `'x'`、`ARG_TYPE` 为 `REQ_ARG`，因此 [src/OptionParser.cc:140-148](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionParser.cc#L140-L148) 生成短选项串时会在 `x` 后追加 `:`（表示必带参数），`getopt_long` 对 `:` 的标准行为就是支持紧贴与分离两种写法。

**练习 2**：为什么配置文件里的未知选项只是 WARN 而命令行上的未知选项却直接报错退出？

**答案**：命令行是用户当下的显式意图，拼错大概率是失误，立即失败最安全（[src/OptionParser.cc:220](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionParser.cc#L220) 抛异常，[src/option_processing.cc:293-297](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/option_processing.cc#L293-L297) 捕获后打印帮助与候选）；配置文件可能由旧版本 aria2 生成、被多个版本共享，硬失败会让升级后的用户寸步难行，所以宽松处理（[src/OptionParser.cc:254](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionParser.cc#L254) 仅 `A2_LOG_WARN`）。这是「同一系统内对不同输入源采取不同严格度」的典型案例。

**练习 3**：`parseArg` 结尾 `std::copy(argv + optind, argv + argc, ...)` 收集的 `nonopts` 最终去了哪里？

**答案**：它们就是命令行上的 URI/种子文件/metalink 文件参数。`option_processing` 的参数 `std::vector<std::string>& uris` 以引用接收，随后 `Context` 构造函数把 `args` 交给后续流程，最终成为 `MultiUrlRequestInfo` 创建 `RequestGroup` 的输入（回顾 u1-l3 第六阶段的四级分支）。选项系统与「要下载什么」在此分道扬镳。

### 4.4 option_processing：四层优先级的总指挥

#### 4.4.1 概念说明

[src/option_processing.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/option_processing.cc#L174-L345) 的 `option_processing()` 是把前面三节所有零件装起来的总装函数，由 `Context` 构造函数第一个调用（[src/Context.cc:153-166](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Context.cc#L153-L166)）。它定下了一条铁律——**选项值按「默认值 < 配置文件 < 环境变量 < 命令行」的优先级合并**，并处理 `--version`/`--help` 提前退出、配置文件解析错误、未知选项的「Did you mean」建议等所有边界情况。

它还体现了一个重要的工程手法：**两遍解析**。读配置文件之前必须先知道 `--no-conf` 和 `--conf-path`（它们自己也是命令行选项），所以第一遍先把命令行文本流解析进一个临时的局部 `Option` 只取这两个值，第二遍才做正式的全量解析。

#### 4.4.2 核心流程

```text
option_processing(op, standalone, uris, argc, argv, options)
│
├─ 第一遍（探路）: parseArg 把 argv 变成 cmdstream 文本流
│    再 parse 进局部 Option，只读 --no-conf / --conf-path
│    standalone 时顺便处理 --version / --help（打印后直接 exit）
│
├─ confOption（配置层 Option）的填充:
│    ① parseDefaultValues: 所有默认值写入
│    ② 若未 --no-conf: 读取配置文件（默认路径或 --conf-path 指定）
│       每行同样走 handler 校验，后写覆盖先写 → 配置文件覆盖默认值
│    ③ overrideWithEnv: http_proxy/https_proxy/ftp_proxy/all_proxy/no_proxy
│       五个环境变量再覆盖 → 环境变量覆盖配置文件
│    ④ 非 standalone（嵌入模式）强制 quiet=true
│
├─ 第二遍（正式）: cmdstream 重绕到开头
│    op.setParent(confOption)      ← 建立分层链
│    parse(op, cmdstream)          ← 命令行值写进 op 本地 → 最高优先级
│    parse(op, options)            ← libaria2 调用方的 KeyVals
│
├─ 异常善后:
│    OptionHandlerException   → 打印异常链 + 该选项的 Usage，返回其错误码
│    UnknownOptionException   → 打印帮助 + showCandidates("Did you mean:")
│    其他 Exception           → 打印栈回溯 + 帮助
│
└─ 尾部修正与检查: --quiet/--stderr 重定向; 无 URI 且无输入文件则报错;
   --daemon 化; --deferred-input 与 --save-session 互斥修正
   返回 FINISHED(0)
```

优先级的实现要点：**不是「后来者显式覆盖」，而是靠 `Option` 的父子链查找顺位**——低优先级层先写入 `confOption`，高优先级的命令行值写入子对象 `op`；`get` 先查子对象本地，查不到才问父亲（4.1 节）。命令行没有指定的键自然「透传」到配置层的值。

#### 4.4.3 源码精读

[src/option_processing.cc:183-219](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/option_processing.cc#L183-L219) —— 第一遍探路：注意第 185 行定义了一个**与外层同名的局部 `Option op`**（作用域到第 219 行为止），故意只保留这几个引导选项；`--help` 的值还会剥掉前导 `--` 和 `=value` 尾巴，得到帮助关键字（如 `split` 或 `#http`）。

[src/option_processing.cc:220-261](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/option_processing.cc#L220-L261) —— 配置层装配：`parseDefaultValues` 先铺默认值；配置文件存在则逐行解析进**同一个** `confOption`（同键后写覆盖先写，故配置文件覆盖默认值）；`--conf-path` 指定的文件不存在会直接报错返回（第 254-260 行），而默认路径不存在则静默跳过。配置文件里的非法值抛 `OptionHandlerException` 时还会顺手打印该选项的 Usage（第 241-245 行）。

[src/option_processing.cc:75-91](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/option_processing.cc#L75-L91) —— `overrideWithEnv`：环境变量存在时把它的值走一遍 handler 校验后写进 `confOption`。结合 [src/option_processing.cc:263-267](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/option_processing.cc#L263-L267) 的五次调用（`http_proxy`/`https_proxy`/`ftp_proxy`/`all_proxy`/`no_proxy`），得出容易被人忽略的事实：**环境变量的优先级高于配置文件**（但低于命令行）。

[src/option_processing.cc:268-281](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/option_processing.cc#L268-L281) —— 收官：`cmdstream.clear()` 清掉 EOF 标志再 `seekg(0)` 重绕文本流（stringstream 读完后需要这两步才能重读，这是本函数最容易被忽略的细节）；`op.setParent(confOption)` 建链；随后命令行与 `KeyVals` 依次写入 `op`。**优先级链在这一刻成型**。

[src/option_processing.cc:283-302](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/option_processing.cc#L283-L302) —— 三种异常的善后：选项值非法时打印异常栈后追加 `Usage:` 与该选项详情（`write` 函数见 4.5 节）；未知选项时打印帮助并调用 `showCandidates`。

[src/option_processing.cc:99-170](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/option_processing.cc#L99-L170) —— 「Did you mean」的实现：`levenshtein` 计算 **Damerau–Levenshtein 距离**（支持相邻交换、替换、增、删四种编辑操作，代价分别取 0/2/1/3，注释说明参数借自 git 的 help.c）；`showCandidates` 对全部公开选项打分——前缀完全匹配记 0 分，否则按编辑距离，取得分最小者集合（阈值 12 以内）作为候选输出。这就是拼错选项时 aria2 能给出聪明建议的原因。

[src/option_processing.cc:303-344](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/option_processing.cc#L303-L344) —— 尾部杂务：`--quiet`/`--stderr` 重定向、无 URI 且无 `-i/-T/--metalink-file` 时报 `MSG_URI_REQUIRED`、`--daemon` 调系统 `daemon()`（macOS 上还用 `#pragma` 压掉了弃用警告）、[第 338-342 行](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/option_processing.cc#L338-L342)处理 `--deferred-input` 与 `--save-session` 的互斥（直接把前者移除并打 WARN——程序化修正选项而非报错的例子）。一切正常返回 `error_code::FINISHED`（0）。

#### 4.4.4 代码实践

1. **实践目标**：用实验验证「命令行 > 环境变量 > 配置文件 > 默认值」的优先级。
2. **操作步骤**（需已编译 aria2c，否则标注待本地验证）：
   - 在临时目录建一个 `aria2.conf`，内容两行：`# 注释行` 与 `max-connection-per-server=8`。
   - 运行 `./src/aria2c --conf-path=<你的路径>/aria2.conf --max-connection-per-server=2 -d . --dry-run https://example.com`，无法直接观察连接数时改用 `-l=- --log-level=info` 开日志，或任选一个能在日志/输出中反映取值的选项（如 `--max-download-limit`）。
   - 再做一组对照：配置文件里写 `max-connection-per-server=8`，命令行不写该选项，环境变量 `all_proxy=http://127.0.0.1:1`（代理是环境变量能覆盖的选项之一），分别观察配置层与环境变量层谁生效（代理生效时下载会尝试连接 127.0.0.1:1，用日志或抓包确认）。
   - 把配置文件里某行改成非法值（如 `max-connection-per-server=99`），重新运行，观察报错格式与退出码（预期同样是 28）。
3. **需要观察的现象**：命令行给值时配置文件的同名值被忽略；命令行不给时配置文件值生效；环境变量能压过配置文件（对代理类选项）；非法配置值会打印 `Parse error in <文件>` 加异常栈。
4. **预期结果**：与 4.4.2 流程图一致；非法配置值路径对应 [src/option_processing.cc:238-247](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/option_processing.cc#L238-L247) 的 catch 分支，退出码为 `OPTION_ERROR`。
5. 若不想破坏下载行为，全部实验都可用 `--dry-run` 或直接以报错退出码为观察对象（如 `echo $?`）。

#### 4.4.5 小练习与答案

**练习 1**：为什么第一遍解析用局部 `Option` 而不直接写进外层的 `op`？

**答案**：因为此刻配置层 `confOption` 还没建好（读哪个配置文件取决于第一遍的结果），若把命令行值直接写进最终的 `op`，会破坏「先低层后高层」的写入次序设计；而且 `--version`/`--help` 分支会直接 `exit`，用局部对象可以保证此时不残留任何半成品状态。等一切就绪后第二遍重放 `cmdstream`（重绕 + `setParent`）才把命令行值写进正式的 `op`。

**练习 2**：`option_processing` 的 `standalone` 参数为 `false` 时（libaria2 嵌入模式）行为有何不同？

**答案**：四处不同——① 不处理 `--version`/`--help`（第 195 行 `if (standalone)`）；② 强制 `quiet=true` 抑制输出（[第 268-273 行](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/option_processing.cc#L268-L273)，注释说明调用方可用 options 参数覆盖）；③ 不做「无 URI 报错」与 `--daemon` 检查（都包在 `if (standalone)` 里）；④ 返回失败时 `Context` 不 `exit` 而是抛异常（[src/Context.cc:159-165](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Context.cc#L159-L165)）——库不能替宿主进程决定生死。

**练习 3**：如果用户同时设置了 `--no-conf` 和 `--conf-path=...`，会发生什么？

**答案**：`--no-conf` 胜出。看 [src/option_processing.cc:222](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/option_processing.cc#L222)：`if (!noConf)` 才会读配置文件，整个配置文件加载分支（包括 `--conf-path` 指定路径的存在性检查）都被跳过，`ucfname` 被静默忽略。

### 4.5 帮助文本生成：usage_text.h、标签与 showUsage

#### 4.5.1 概念说明

`aria2c -h` 的输出两百多个选项、几千行文本，但 aria2 没有一行硬编码的「帮助文档」——**帮助文本完全是运行时从选项注册表里长出来的**。三个零件：

1. **`usage_text.h` 的 `TEXT_*` 宏**：每个选项的说明文字单独一个宏，注册时注入处理器（`description_` 字段）。
2. **标签（tag）体系**：`help_tags.h` 定义 15 个标签（basic/advanced/http/…/help），处理器用位标志持有多个标签；`--help` 的值就是过滤条件（`#标签名` 或任意子串）。
3. **`showUsage` + `write`**：按标签或子串从 `OptionParser` 查出处理器集合，逐个格式化打印「说明 + 可取值 + 默认值 + 标签」。

这套机制让「新增一个选项」只需要动三个文件（`prefs.cc` 登记、`OptionHandlerFactory.cc` 注册、`usage_text.h` 写说明），帮助输出自动跟上——这也是 u9-l6 二次开发讲义要复用的结论。

#### 4.5.2 核心流程

```text
用户敲 aria2c -h  （即 --help，参数可省略）
  └─ 第一遍解析发现 PREF_HELP 已定义
       值为空 → keyword = "#basic"（默认标签）
       值非空 → 剥掉 "--" 前缀与 "=..." 尾巴 → keyword
  └─ showUsage(keyword, oparser, cout)
       keyword 为空     → 只打一行 "See 'aria2c -h'."
       keyword 以 # 开头
          == "#all"     → findAll() 全部公开选项
          否则          → findByTag(idHelpTag(keyword))
       否则             → findByNameSubstring(keyword) 模糊匹配
       对每个 handler 调 write(): 打印
          description（来自 TEXT_* 宏）
          Possible Values: createPossibleValuesString()（如 1-16）
          Default:         默认值（如 1）
          Tags:            toTagString()（如 basic, ftp, http）
  └─ exit(FINISHED)
```

#### 4.5.3 源码精读

[src/usage_text.h:742-744](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/usage_text.h#L742-L744) —— `TEXT_MAX_CONNECTION_PER_SERVER` 宏：注意格式约定——首行 ` -x, --max-connection-per-server=NUM`，续行对齐到第 30 列。文件开头（[src/usage_text.h:36](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/usage_text.h#L36)）有 `// clang-format off`，因为手工排版不希望被格式化工具重排。

[src/OptionHandler.cc:67-78](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandler.cc#L67-L78) —— `write`：四段式输出（说明 / 可取值 / 默认值 / 标签）。`createPossibleValuesString()` 由各处理器子类实现，如 `NumberOptionHandler` 按 min/max 生成 `1-16`（[src/OptionHandlerImpl.cc:187-203](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerImpl.cc#L187-L203)），无界一侧用 `*`。你在 4.2 实践中看到的 `Possible Values: 1-16` 就来自这里。

[src/version_usage.cc:84-140](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/version_usage.cc#L84-L140) —— `showUsage` 的三分支路由：空关键字一行提示；`#` 开头按标签（`#all` 特判走 `findAll`）；否则按名字子串。第 138 行还有一个兜底：子串匹配不到任何选项时，打印 `--help` 自己的说明教用户怎么用帮助。

[src/version_usage.cc:141-180](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/version_usage.cc#L141-L180) —— 当关键字是 `#basic` 时额外附加一段「URI, MAGNET, TORRENT_FILE, METALINK_FILE」用法说明，其中 [第 171-179 行](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/version_usage.cc#L171-L179) 正是著名的提示「默认每主机 1 连接，想回到 1.9.x 行为请用 `--max-connection-per-server=4 --min-split-size=1M`」——这段静态文本与本讲主角选项直接相关。

[src/help_tags.h:42-61](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/help_tags.h#L42-L61) —— `HelpTag` 枚举共 15 个标签（TAG_BASIC 到 TAG_HELP），另有特殊字符串 `STR_TAG_ALL("#all")`。`strHelpTag`/`idHelpTag` 负责枚举与字符串互转。

[src/OptionHandlerFactory.cc:1980-1994](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc#L1980-L1994) —— `--help` 自己也是一个选项：`DefaultOptionHandler`，**默认值就是 `strHelpTag(TAG_BASIC)`**（所以 `-h` 不带参数等价于 `-h#basic`），可取值字符串 dynamically 拼出全部标签加 `#all`。紧邻其上（[src/OptionHandlerFactory.cc:1972-1978](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc#L1972-L1978)）是 `--version`，`NO_ARG` 型。

[src/AbstractOptionHandler.cc:80-93](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractOptionHandler.cc#L80-L93) —— `toTagString`：把位标志还原成逗号分隔的标签串，供 `Tags:` 行使用。同时它也解释了 [src/OptionParser.cc:300-309](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionParser.cc#L300-L309) `findByTag` 能一次命中多标签选项的原因（一个比特位一个标签）。

#### 4.5.4 代码实践

1. **实践目标**：体验帮助系统的三种查询方式，并验证「帮助 = 注册表投影」。
2. **操作步骤**：
   - 运行 `./src/aria2c -h | head -40`、`./src/aria2c -h#http | head -40`、`./src/aria2c --help=max-connection | tail -20`（需已编译；否则待本地验证，但仍可纯读码回答第 4 步）。
   - 在 `--help=max-connection` 的输出中找到 `max-connection-per-server` 条目，核对四段内容。
   - 数一数 `aria2c -h` 与 `aria2c -h#all` 的输出行数差异。
3. **需要观察的现象**：`-h` 默认只显示 `#basic` 标签的选项（几十个）；`#all` 显示全部约 200 个；`--help=max-connection` 只显示名字含该子串的少数选项，且每个条目末尾有 `Default:` 与 `Tags:` 行。
4. **预期结果**：`max-connection-per-server` 条目应显示 `Possible Values: 1-16`、`Default: 1`、`Tags: basic, ftp, http`——与 [src/OptionHandlerFactory.cc:438-449](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc#L438-L449) 的注册信息一字不差。帮助文本没有任何独立文档源，全部是注册表的运行时投影。
5. 附加：试运行 `./src/aria2c --halp`（故意拼错），观察末尾的 `Did you mean:` 候选列表，回到 [src/option_processing.cc:127-170](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/option_processing.cc#L127-L170) 对照打分规则（`--halp` 与 `--help` 的 Damerau–Levenshtein 距离很小——一次相邻字符交换，代价仅 0）。

#### 4.5.5 小练习与答案

**练习 1**：`aria2c -h` 与 `aria2c --help=#all` 输出的选项数量为什么不同？多出来的选项是哪些？

**答案**：`-h` 默认关键字是 `#basic`，只显示打了 `TAG_BASIC` 标签的选项；`#all` 走 `findAll()` 显示所有未隐藏选项。多出来的是只打了 advanced/http/bittorrent 等其他标签的选项。另外注意 `isHidden()` 的选项（如内部弃用选项）在任何帮助下都不显示（[src/OptionParser.cc:327-336](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionParser.cc#L327-L336) 注释 "Hidden options are not returned"）。

**练习 2**：如果给 `--help` 传一个不存在的标签 `#foo`，会发生什么？

**答案**：`idHelpTag("#foo")` 返回 `MAX_HELP_TAG`，`findByTag(MAX_HELP_TAG)` 中 `hasTag(1 << MAX_HELP_TAG)` 对任何处理器都不成立（位标志只有低 15 位有效），于是返回空集合、什么都不打印——既不报错也不显示帮助。这是第 138 行兜底逻辑覆盖不到的角落（兜底只处理非 `#` 关键字分支）。

**练习 3**：帮助文本为什么放在独立的 `usage_text.h` 而不是直接写在 `OptionHandlerFactory.cc` 的注册处？

**答案**：分离「注册逻辑」与「长文本」。`usage_text.h` 顶部标了 `clang-format off` 保留手工列对齐，而工厂文件可以正常自动格式化；同时这些宏包着 `_()` 国际化标记，集中存放便于翻译流程批量处理。若混在一起，近 2000 行的工厂函数会膨胀到难以检索。

## 5. 综合实践

**任务：为 `--max-connection-per-server` 建立一份完整的「选项档案」，并做一次优先级实验。**

把本讲五个模块串起来，完成下面四步：

1. **查档案（纯读码，约 15 分钟）**：对着源码填写下表（答案见 4.2.4，可先遮住再做）。

   | 档案项 | 值 | 源码位置 |
   | --- | --- | --- |
   | 键名常量 | `PREF_MAX_CONNECTION_PER_SERVER` | `prefs.cc:316` |
   | 处理器类型 | `NumberOptionHandler` | `OptionHandlerFactory.cc:439` |
   | 默认值 | `"1"` | `OptionHandlerFactory.cc:441` |
   | 取值范围 | `1-16` | `OptionHandlerFactory.cc:441` |
   | 短选项 | `-x` | `OptionHandlerFactory.cc:441` |
   | 帮助文本宏 | `TEXT_MAX_CONNECTION_PER_SERVER` | `usage_text.h:742` |
   | 标签 | basic, ftp, http | `OptionHandlerFactory.cc:442-444` |

2. **验证校验（需编译）**：运行 `./src/aria2c --max-connection-per-server=99`，记录报错文本与 `echo $?` 的退出码（预期 28），然后不看讲义复述五步异常链（`NumberOptionHandler::parseArg` 范围检查 → `AbstractOptionHandler::parse` 包装 → `option_processing` 捕获打印 Usage → 返回 28 → `Context::exit`）。再运行 `./src/aria2c -x16 --dry-run https://example.com` 确认上界 16 合法、`-x` 短选项可用。

3. **验证优先级（需编译）**：建配置文件写 `max-connection-per-server=8`，分别以「只用配置文件」「配置文件 + 命令行 `-x2`」两种方式运行（用 `-l=- --log-level=info` 辅助观察），确认命令行值胜出；再设 `all_proxy=http://127.0.0.1:9` 环境变量与配置文件里的代理值做一组对照，确认环境变量胜过配置文件。若某些取值无法直接观察，允许改用行为可观察的等价选项（如 `--max-download-limit`、`all_proxy`）并说明替换理由。

4. **画一张数据流图**：以 `--max-connection-per-server=4` 为例，画出从 `argv[i]` → `getopt_long` → `cmdstream`（`"max-connection-per-server=4\n"`）→ `k2p` → `NumberOptionHandler::parseArg` → `Option::put`（`table_[ID] = "4"`）→ 后续某处 `getAsInt(PREF_MAX_CONNECTION_PER_SERVER)` 读回 4 的完整链路，并在每个箭头旁标注文件名与函数名。

**验收标准**：表格全部正确；退出码 28 与异常链复述无误；优先级实验现象与 4.4.2 流程图一致；数据流图能落到具体函数。

## 6. 本讲小结

- aria2 的选项世界由五份文件分工：`prefs.cc` 登记键名发 ID、`OptionHandlerFactory.cc` 给每个键配校验器与元数据、`usage_text.h` 存帮助文本、`Option.cc` 以「ID 为下标的数组 + 位图 + 父链」存值、`option_processing.cc` 总装合并——查任何选项按此顺序检索。
- 值的合并遵循「内置默认值 < 配置文件 < 环境变量 < 命令行」，实现方式不是显式覆盖而是 `Option` 的父子链查找：低层先写入 `confOption`，命令行写子对象，`get` 先本地后父亲。
- `OptionParser` 把 `argv` 交给 `getopt_long` 后先产出 `NAME=VALUE` 文本流再交给处理器，使命令行、配置文件、libaria2 的 `KeyVals` 三种来源共用同一条校验通道；GNU `getopt_long` 还顺带提供了长选项无歧义缩写能力。
- `OptionHandler` 是模板方法模式：基类 `parse` 固定 try/catch 骨架并把子类异常包装成错误码 28（`OPTION_ERROR`）的 `OptionHandlerException`；类型校验在具体子类（如 `NumberOptionHandler` 的 1-16 范围检查）中完成，`--max-connection-per-server=99` 的报错文本正出自 [src/OptionHandlerImpl.cc:176](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerImpl.cc#L176)。
- 帮助输出没有独立文档源，是注册表的运行时投影：标签体系（15 个 tag 的位标志）支撑 `-h`/`-h#http`/`--help=子串` 三种过滤；未知选项还有基于 Damerau–Levenshtein 距离的「Did you mean」建议。
- 选项在 `Option` 内部一律存字符串，类型转换推迟到 `getAsInt`/`getAsBool` 等读取函数；入口处的处理器校验保证了存储层永远拿到合法字符串。

## 7. 下一步学习建议

- **下一讲（u2-l1）DownloadEngine：事件驱动主循环**——选项解析完成、`Context` 构造完毕之后，`MultiUrlRequestInfo::prepare()` 会把 `Option` 交给 `DownloadEngineFactory` 装配引擎。你将看到本讲的 `option->getAsInt(PREF_...)` 在引擎初始化中被大量调用，选项如何真正「驱动」行为。
- **提前留意 u3-l3（download_helper 任务创建流水线）**：本讲 4.3 节收尾提到的 `nonopts`（URI 列表）与 `Option` 如何一起变成 `RequestGroup`，是选项系统的直接下游。
- **如果你对「加一个新选项要改哪些文件」感兴趣**：直接跳读 u9-l6（二次开发实战），它把本讲得出的「prefs 登记 → 工厂注册 → 帮助宏」三步套路变成一次完整动手实践。
- **推荐继续精读的源码**：`src/OptionHandlerImpl.cc` 全文（约 400 行，能看全 10 余种校验器的写法）；`src/option_processing.cc` 的异常分支（对照 `src/error_code.h` 的 33 个退出码，为 u9-l3 日志与异常讲义做准备）。
