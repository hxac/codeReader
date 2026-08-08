# DSLX 前端：扫描、解析与 AST

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚一段 DSLX 源码文本，是如何先被「扫描（scan）」成一串 Token，再被「递归下降解析（parse）」成一棵抽象语法树（AST）的。
- 在 `xls/dslx/frontend/ast.h` 里认得函数、参数、`let`、`match`、语句块等核心 AST 节点，并知道它们如何通过 `Module::Make` 这个工厂方法被分配与回收。
- 理解 `Module` 作为顶层编译单元的结构：它用 `ModuleMember` 这个 `variant` 统一持有函数、测试、结构体、`import` 等顶层成员。
- 跟踪一条 `let (a, b) = ...;` 语句，从解析函数一路读到它构造出的 `Let` + `TuplePattern` + `NameDef` 子树。

本讲聚焦的是 DSLX 前端的**前半段**：源码进，AST 出。AST 出来之后才会交给类型系统（下一讲 u2-l3）和 IR 转换（u3-l4）继续处理。

## 2. 前置知识

在进入源码前，先用通俗语言把几个关键概念说清楚。

### 什么是前端、扫描、解析、AST

编译器通常分「前端—中端—后端」。对 XLS 来说，**前端**负责把人写的 DSLX 文本变成机器内部的数据结构。这个过程分两步：

1. **词法扫描（lexing / scanning）**：把连续的字符流切成一个个有意义的「单词」，叫 **Token**。比如 `let x = u8:3;` 会被切成 `let`、`x`、`=`、`u8`、`:`、`3`、`;` 七个 Token。
2. **语法解析（parsing）**：按语言的语法规则，把这些 Token 组织成一棵树，叫 **抽象语法树（Abstract Syntax Tree, AST）**。树根通常是整个模块，叶子是最小的语法单元。

打个比方：扫描像把一句话拆成「词」，解析像按语法把这些词拼成「句子结构图」。

### 递归下降解析器（recursive descent parser）

DSLX 用的是**手写**的递归下降解析器：每个语法结构（函数、表达式、`let`、`match`……）都对应一个 `ParseXxx` 成员函数；当某结构里又嵌套别的结构时，就递归调用对应的函数。它不依赖语法生成工具（如 yacc/bison），而是直接用 C++ 的函数调用栈来表达嵌套。

### Token 的「前瞻」与「消费」

解析器读 Token 时有两种基本动作：

- **前瞻（peek）**：看一眼当前 Token 是什么，但不把它从流里拿走。常用于「先看一眼再决定走哪个分支」。
- **消费（pop / drop）**：把当前 Token 从流里取走，确认它就是我们期待的。

DSLX 解析器大量使用这两个动作：先 `PeekToken()` 看下一个 Token 的种类，再决定调用哪个 `ParseXxx`；进入分支后再用 `PopToken` / `DropTokenOrError` 消费掉起始关键字（如 `fn`、`let`、`match`）。

### Span 与 Pos：源码定位

为了让报错信息能精确到「第几行第几列」，每个 Token、每个 AST 节点都带一个 **Span**（一段区间），由起点 **Pos** 和终点 **Pos** 组成。`Pos` 记录的是 `(文件号, 行号, 列号)`。这是后续语言服务器（LSP）、错误高亮、调试器的基础。

### 变体（variant）与访问者（visitor）

源码里会反复出现两个 C++ 技巧，先记住它们的用途即可，不必深究语法：

- `std::variant<A*, B*, C*>`：一个「可以装 A、B、C 三者之一」的指针容器。比如 `ModuleMember` 就是一个 variant，一个模块成员「可能是函数，也可能是测试，也可能是 import」。
- **访问者模式（visitor）**：对一棵 AST，用一个 `AstNodeVisitor`，针对每种节点类型提供一个 `HandleXxx` 回调。好处是不用满屏 `switch-case` 和 `dynamic_cast`。

## 3. 本讲源码地图

本讲涉及的关键文件都集中在 `xls/dslx/frontend/` 下：

| 文件 | 作用 |
| --- | --- |
| `token.h` | 定义 `Token`、`TokenKind`（Token 种类枚举）、`Keyword`（关键字枚举）。 |
| `scanner.h` / `scanner.cc` | `Scanner` 把文本字符流切分成 Token 流。 |
| `scanner_keywords.inc` | 用宏罗列全部关键字（`fn`、`let`、`match`、`u8`……），扫描器和解析器共用。 |
| `token_parser.h` | `TokenParser` 基类，提供 `PeekToken`/`PopToken`/`DropToken` 等通用 Token 操作。 |
| `bindings.h` | `Bindings` 类，解析期维护「名字 → 定义点」的词法作用域链。 |
| `parser.h` / `parser.cc` | `Parser`，DSLX 的递归下降解析器主体（约 4900 行）。 |
| `ast.h` | 全部 AST 节点类的定义：`Function`、`Param`、`Let`、`Match`、`NameDef`…… |
| `ast_node.h` | `AstNode` 抽象基类与 `AstNodeKind` 枚举。 |
| `module.h` | `Module` 顶层编译单元，持有所有顶层成员并管理全部 AST 节点的内存。 |
| `pos.h` | `Pos`（位置）与 `Span`（区间）定义。 |

记忆线索：**文本 → Scanner → Token → TokenParser(基类) → Parser(递归下降) → AST 节点(ast.h) → 挂到 Module(module.h)**。本讲按这条线从左到右讲。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**词法扫描**、**递归下降解析器**、**AST 节点体系**、**Module 顶层组织**。

### 4.1 词法扫描：从文本到 Token

#### 4.1.1 概念说明

词法扫描器（`Scanner`）是前端的「读字工」。它的输入是一整段 DSLX 源码文本，输出是一个 Token 序列。每个 Token 携带三样东西：

1. **种类（`TokenKind`）**：它是哪一类符号——是关键字、标识符、数字、还是某个标点（`(`、`)`、`{`、`+`……）。
2. **区间（`Span`）**：它在源码里的位置，用于报错。
3. **载荷（payload）**：可选的字符串或关键字值。比如数字 Token 装着 `"42"`，关键字 Token 装着 `Keyword::kLet`。

一个常被忽略的细节：DSLX 把 `u8`、`u32`、`s64` 这类内置类型名也当作**关键字**（而不是普通标识符）来扫描。这意味着你不能把函数参数命名为 `u8`。这点在 `scanner_keywords.inc` 里能直接看到。

#### 4.1.2 核心流程

扫描的本质是一个有限状态机：逐字符读取，根据当前字符决定进入哪个状态。

```
源码文本 "let x = u8:3;"
   │  逐字符扫描
   ▼
Token 流:
  [Keyword(let)] [Identifier("x")] [Equals] [Keyword(u8)] [Colon] [Number("3")] [Semi]
```

扫描器的对外接口很简单：它内部一次性把整段文本切成 Token 缓存起来（`tokens_`），解析器再通过下标一个一个往前取。这种「先全量切好，再顺序消费」的做法，让解析器可以自由地向前看（peek）多个 Token，而不用每次都重新跑扫描逻辑。

`Token` 的构造清晰地体现了「种类 + 区间 + 载荷」三件套：

```cpp
// token.h:110-117  —— Token 的核心构造
class Token {
 public:
  Token(TokenKind kind, Span span,
        std::optional<std::string> value = std::nullopt)
      : kind_(kind), span_(std::move(span)), payload_(value) {}

  Token(Span span, Keyword keyword)
      : kind_(TokenKind::kKeyword), span_(std::move(span)), payload_(keyword) {}
```

#### 4.1.3 源码精读

**Token 种类枚举**。全部 Token 种类用一个大宏罗列，既生成枚举、又生成字符串转换、还导出给 Python 绑定复用。标点、括号、操作符都是独立的 `TokenKind`：

```cpp
// token.h:35-87（节选）  —— Token 种类：标点/操作符/括号/标识符/数字……
#define XLS_DSLX_TOKEN_KINDS(X)                                        \
  X(kDot, DOT, ".")                                                    \
  X(kEof, EOF, "EOF")                                                  \
  X(kKeyword, KEYWORD, "keyword")                                      \
  X(kIdentifier, IDENTIFIER, "identifier")                             \
  X(kNumber, NUMBER, "number")                                         \
  X(kOParen, OPAREN, "(")    X(kCParen, CPAREN, ")")                   \
  X(kOBrace, OBRACE, "{")     X(kCBrace, CBRACE, "}")                  \
  X(kEquals, EQUALS, "=")     X(kColon, COLON, ":")                    \
  X(kSemi, SEMI, ";")         X(kFatArrow, FAT_ARROW, "=>")            \
  X(kBar, BAR, "|")           X(kOAngle, OANGLE, "<")                  \
  ...
```

这里能看到本讲后面会反复出现的几个：`kOParen`/`kCParen`（`(`/`)`）、`kOBrace`/`kCBrace`（`{`/`}`）、`kEquals`（`=`）、`kColon`（`:`）、`kSemi`（`;`）、`kFatArrow`（`=>`，match 用）、`kBar`（`|`，多模式分隔）。

参见 [xls/dslx/frontend/token.h:L35-L87](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/frontend/token.h#L35-L87)：这就是 DSLX 词法的「字母表」。

**关键字枚举**。关键字是 `TokenKind::kKeyword` 下的一种，再用 `Keyword` 二级枚举区分具体是哪个。注意 `u8`/`u32`/`s64` 等「带位宽的类型名」和 `fn`/`let`/`match` 一样都是关键字：

```cpp
// scanner_keywords.inc:19-43  —— 关键字列表（节选）
#define XLS_DSLX_KEYWORDS(X)               \
  X(kAs, AS, "as")          X(kConst, CONST, "const")   \
  X(kElse, ELSE, "else")    X(kFn, FN, "fn")            \
  X(kFor, FOR, "for")       X(kIf, IF, "if")            \
  X(kLet, LET, "let")       X(kMatch, MATCH, "match")   \
  X(kPub, PUB, "pub")       X(kProc, PROC, "proc")      \
  X(kStruct, STRUCT, "struct")  X(kType, TYPE, "type")  \
  ...
```

参见 [xls/dslx/frontend/scanner_keywords.inc:L19-L43](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/frontend/scanner_keywords.inc#L19-L43)：本讲涉及的 `fn`、`let`、`match`、`pub`、`const` 都在这里。

**Scanner 的构造**。`Scanner` 持有整段文本和一个可选的「是否保留空白/注释」开关（语言服务器做语法高亮时需要空白和注释 Token，普通编译时丢弃它们）：

```cpp
// scanner.h:85-90  —— Scanner 持有源码文本与配置
Scanner(FileTable& file_table, Fileno fileno, std::string text,
        bool include_whitespace_and_comments = false)
    : file_table_(file_table),
      fileno_(fileno),
      text_(std::move(text)),
      include_whitespace_and_comments_(include_whitespace_and_comments) {}
```

参见 [xls/dslx/frontend/scanner.h:L85-L90](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/frontend/scanner.h#L85-L90)。扫描阶段我们一般不直接调用 `Scanner`，它由 `Parser` 构造时接管。

#### 4.1.4 代码实践

**实践目标**：建立「文本 → Token」的直觉，并验证 `u8` 等是关键字。

**操作步骤**：

1. 打开 `xls/examples/gcd.x`，看第 20 行 `fn gcd_euclidean<N: u32, ...>(a: uN[N], b: uN[N]) -> uN[N] {`。
2. 手工把这一行的 Token 列出来，标注每个 Token 的 `TokenKind`（例如 `fn`→`kKeyword(kFn)`，`gcd_euclidean`→`kIdentifier`，`<`→`kOAngle`，`N`→`kIdentifier`，`:`→`kColon`，`u32`→`kKeyword(kU32)`）。
3. 在 `scanner_keywords.inc` 里逐一确认你标的关键字确实在表里。

**需要观察的现象**：你会发现 `uN`、`u32`、`N` 三者「看起来都是标识符」，但只有 `N` 是真正的 `kIdentifier`，`uN`/`u32` 是关键字。这正是为什么 DSLX 不允许把变量命名为 `u32`。

**预期结果**：手工 Token 化结果应与上一步描述一致。完整扫描的精确顺序取决于扫描器实现，可标注「待本地验证」（例如用调试器或加打印确认 `tokens_` 内容）。

#### 4.1.5 小练习与答案

**练习 1**：DSLX 里 `=>` 和 `=` 是同一个 Token 吗？分别用在什么语法里？

> **答案**：不是。`=>` 是 `TokenKind::kFatArrow`，用于 `match` 分支的「模式 => 表达式」；`=` 是 `TokenKind::kEquals`，用于 `let` 赋值。两者在 `token.h` 的宏里是独立条目。

**练习 2**：为什么扫描器要把 `u32` 这种类型名也当成关键字，而不是当普通标识符？

> **答案**：把内置类型名设为关键字，可以让扫描/解析阶段直接区分「用户自定义名」和「内置类型」，避免歧义（例如禁止用 `u32` 作变量名），也简化了类型注解的解析。代价是关键字表变长，所以项目用一个 `.inc` 宏集中维护。

---

### 4.2 递归下降解析器：Parser 与 Bindings

#### 4.2.1 概念说明

`Parser` 是前端的「组句工」。它继承自 `TokenParser`（提供 `PeekToken`/`PopToken`/`DropToken` 等通用 Token 操作），并实现了一大堆 `ParseXxx` 方法，每方法对应一条语法规则。例如：

- `ParseModule()`：解析整个模块（顶层成员循环）。
- `ParseFunction()` / `ParseFunctionInternal()`：解析 `fn ...`。
- `ParseExpression()`：解析一个表达式，内部按优先级分派到 `ParseLet`、`ParseFor`、`ParseMatch`、`ParseBinopChain` 等。
- `ParseLet()`：解析 `let ... = ...;`。
- `ParseMatch()`：解析 `match ... { ... }`。

解析器在解析时还要维护一张**符号表**，叫 `Bindings`：它记录「当前作用域里，某个名字（如 `a`、`gcd`）是在哪个 AST 节点定义的」。遇到对名字的引用（`NameRef`），就能查到它的定义点（`NameDef`），从而在后续类型检查阶段把两者关联起来。`Bindings` 是**链式的**（有 `parent` 指针），天然对应词法作用域的嵌套——进入函数体就新建一层，函数结束就丢弃这一层。

> 术语：**词法作用域（lexical scope）**指「按代码书写的嵌套结构决定名字可见性」，DSLX 与 Rust/Python 一样采用这套规则。

#### 4.2.2 核心流程

解析器的整体节奏是「循环消费顶层成员」：

```
ParseModule:
  预置内置名字（range、u8/u16/...）进 Bindings
  while 还没到文件尾:
      先看 Token：
        '#'        -> 解析属性 #[test] / 模块属性 #![...]
        'pub'      -> 标记接下来的成员是公开的
        关键字 fn  -> ParseFunction，得到 Function*
        关键字 proc-> ParseProc
        ...
      把得到的 ModuleMember 加入 Module（module_->AddTop）
```

对于表达式，`ParseExpression` 用「先 peek 再分派」的策略决定走哪条语法分支。`Parser` 还用一个 RAII 守卫 `ExpressionDepthGuard` 来限制表达式嵌套深度，防止恶意/超深嵌套输入导致栈溢出。

`Bindings` 的链式查找逻辑很直观——从当前层往上逐层找，直到找到或到顶：

```
ResolveNode("a"):
  for 当前 Bindings 层 b = this; b != null; b = b.parent:
      若 b 的本地映射里有 "a": 返回它
  返回「找不到」
```

#### 4.2.3 源码精读

**Parser 的继承与构造**。`Parser` 继承 `TokenParser`，构造时就会创建一个 `Module` 作为目标 AST 容器，并保存扫描器指针：

```cpp
// parser.h:113-120  —— Parser 继承 TokenParser，自建 Module
class Parser : public TokenParser {
 public:
  Parser(std::string module_name, Scanner* scanner, bool parse_fn_stubs = false)
      : TokenParser(scanner),
        owned_module_(new Module(std::move(module_name), scanner->filename(),
                                 scanner->file_table())),
        module_(owned_module_.get()),
        parse_fn_stubs_(parse_fn_stubs) {}
```

参见 [xls/dslx/frontend/parser.h:L113-L120](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/frontend/parser.h#L113-L120)。注意：`Module` 是 `Parser` 自己 `new` 出来并持有的（`owned_module_`），解析完成后通过 `ParseModule()` 把所有权交还给调用方。

**TokenParser 的通用操作**。`TryPopToken` 是「尝试消费一个指定种类的 Token，不是就返回 nullopt」的典型实现——先 peek，匹配才 pop：

```cpp
// token_parser.h:82-88  —— 先看后取的通用 Token 操作
absl::StatusOr<std::optional<Token>> TryPopToken(TokenKind target) {
  XLS_ASSIGN_OR_RETURN(const Token* peek, PeekToken());
  if (peek->kind() == target) {
    return PopTokenOrDie();
  }
  return std::nullopt;
}
```

参见 [xls/dslx/frontend/token_parser.h:L82-L88](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/frontend/token_parser.h#L82-L88)。`Parser` 里到处用的 `TryDropToken`、`DropTokenOrError` 都建立在这类「peek + 条件 pop」之上。

**ParseModule 的顶层循环**。这是整个解析的入口。它先把内置类型关键字和 parametric 内置函数预置进 `Bindings`，然后循环处理属性、`pub`、各种顶层构造：

```cpp
// parser.cc:449-456、462-465  —— ParseModule 入口：建立词法环境
absl::StatusOr<std::unique_ptr<Module>> Parser::ParseModule(Bindings* bindings) {
  const Pos module_start_pos = GetPos();
  std::optional<Bindings> stack_bindings;
  if (bindings == nullptr) {
    stack_bindings.emplace();
    bindings = &*stack_bindings;
  }
  ...
  for (auto const& it : GetParametricBuiltins()) {
    std::string name(it.first);
    bindings->Add(name, module_->GetOrCreateBuiltinNameDef(name));
  }
```

参见 [xls/dslx/frontend/parser.cc:L449-L465](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/frontend/parser.cc#L449-L465)：这里能看到「预置内置名字」的动作——这就是为什么 DSLX 里能直接用 `range`、`u8` 等。

循环主体里，它用 `PeekToken` 决定分支，例如看到 `#` 处理属性、看到 `pub` 标记公开、否则按关键字分派到具体 `ParseXxx`。参见 [xls/dslx/frontend/parser.cc:L499-L558](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/frontend/parser.cc#L499-L558)。

**Bindings 的链式查找**。`ResolveNode` 沿 `parent_` 链向上找，完美对应词法作用域：

```cpp
// bindings.h:219-227  —— 名字解析：沿作用域链向上找
std::optional<BoundNode> ResolveNode(std::string_view name) const {
  for (const Bindings* b = this; b != nullptr; b = b->parent_) {
    auto it = b->local_bindings_.find(name);
    if (it != b->local_bindings_.end()) {
      return it->second;
    }
  }
  return std::nullopt;
}
```

参见 [xls/dslx/frontend/bindings.h:L219-L227](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/frontend/bindings.h#L219-L227)。当一个子作用域解析结束，`ConsumeChild` 会把子层新加的绑定「上交」给父层（见 [xls/dslx/frontend/bindings.h:L137-L140](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/frontend/bindings.h#L137-L140)），这是 `ParseLet` 里让 `let` 引入的名字在后续语句中可见的关键。

**表达式深度的 RAII 守卫**。`ExpressionDepthGuard` 利用对象析构自动减计数，避免忘记回退：

```cpp
// parser.h:88-109（节选）  —— 限制表达式嵌套深度，防栈溢出
class ABSL_MUST_USE_RESULT ExpressionDepthGuard final {
 public:
  explicit ExpressionDepthGuard(Parser* parser) : parser_(parser) {}
  ~ExpressionDepthGuard();
  // move-only；拷贝被删除
 private:
  Parser* parser_;
};
```

参见 [xls/dslx/frontend/parser.h:L88-L109](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/frontend/parser.h#L88-L109)。每个 `ParseExpression` 调用开头都会 `BumpExpressionDepth()` 拿到一个守卫（见 [xls/dslx/frontend/parser.cc:L1353](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/frontend/parser.cc#L1353)），嵌套太深就返回错误。

#### 4.2.4 代码实践

**实践目标**：理解「peek 分派 + drop 消费」的解析模式。

**操作步骤**：

1. 打开 `parser.cc` 的 `ParseExpression`（[xls/dslx/frontend/parser.cc:L1328-L1372](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/frontend/parser.cc#L1328-L1372)）。
2. 找到那段连续的 `if (peek->IsKeyword(...))` / `if (peek->kind() == ...)` 分派，记录它把哪些起始 Token 路由到了哪个 `ParseXxx`：
   - `for` / `unroll_for` → `ParseFor`
   - `chan` → `ParseChannelDecl`
   - `spawn` 标识符 → `ParseSpawn`
   - `{` → `ParseBlockExpression`
   - `|` / `||` → `ParseLambda`
3. 思考：为什么是「先 peek 再分派」，而不是直接 pop？

**需要观察的现象**：解析器在确定走哪个分支**之前**不会消费 Token；只有进入具体分支（如 `ParseFor`）后，才会 `PopKeywordOrError(Keyword::kFor)` 把 `for` 消费掉。

**预期结果**：你能画出一张「起始 Token → ParseXxx 函数」的对照表。这种「看一眼再决定」是递归下降解析器的标志，也让回溯（checkpoint 保存/恢复）成为可能。

#### 4.2.5 小练习与答案

**练习 1**：`Bindings` 为什么要做成带 `parent` 指针的链表，而不是一张全局大表？

> **答案**：因为 DSLX 有词法作用域嵌套（函数体内、`for` 体内、`match` 分支内各是一层）。链表能精确表达「内层同名变量遮蔽外层」，且函数/块结束时只需丢弃内层，不影响外层。全局大表无法表达遮蔽，也难以及时回收。

**练习 2**：`ExpressionDepthGuard` 用 RAII（构造加、析构减）来管理深度。如果改成「手动加一、函数末尾手动减一」，会有什么风险？

> **答案**：只要有任何提前 `return`（尤其是错误返回 `XLS_RETURN_IF_ERROR`）忘了减一，深度计数就会泄漏式增长，最终把正常的表达式也误判为「过深」。RAII 借助析构保证每条返回路径都会减一，更安全。

---

### 4.3 AST 节点体系

#### 4.3.1 概念说明

AST 是前端的产物，也是后续所有阶段（类型检查、IR 转换、字节码发射）共同消费的数据结构。XLS 的 AST 节点体系有清晰的分层：

- **`AstNode`** 是所有节点的抽象基类（定义在 `ast_node.h`），每个节点有一个 `AstNodeKind` 枚举值标识种类。
- 节点分成两大类：**表达式节点（`Expr`）**——它们「有值」，可以出现在需要值的位置；以及**非表达式节点**——如 `Function`、`Param`、`Let`、`NameDef`、`Attribute`、各种 `TypeAnnotation`（类型注解）。
- 全部叶子节点类型用两个宏罗列：`XLS_DSLX_EXPR_NODE_EACH`（所有 `Expr` 子类）和 `XLS_DSLX_AST_NODE_EACH`（所有 AST 节点，含上面的 Expr）。

> 术语：**类型注解（TypeAnnotation）**是写在 `:` 后面的类型说明，如 `a: u8` 里的 `u8`。它本身不是表达式，是一类独立节点。

一个关键设计：**所有 AST 节点都没有拷贝语义，且由 `Module` 集中持有内存**。你不能 `new` 一个 `Let`，必须 `module_->Make<Let>(...)`。这样 `Module` 析构时能统一释放全部节点，避免内存泄漏和悬空指针。

#### 4.3.2 核心流程

一个典型的语法结构如何变成 AST 节点，以 `let (a, b) = (1, 2);` 为例：

```
文本:  let ( a , b ) = ( 1 , 2 ) ;
        │   └─TuplePattern─┘   │ └──XlsTuple──┘ │
        ▼                      ▼                ▼
ParseLet:
  1. Pop 起始关键字 let          (const_ = false)
  2. 看到 '('  -> ParseNameDefPattern：
        新建 TuplePattern，内含两个 NameDef("a")、NameDef("b")
  3. Drop '=' 
  4. ParseExpression -> 得到 XlsTuple(Number(1), Number(2)) 作为 rhs
  5. Drop ';'
  6. module_->Make<Let>(span, pattern=TuplePattern, type=null, rhs, const_=false)

最终子树:
        Let
       /  |  \
  pattern type rhs
  TuplePattern  XlsTuple
   /    \        /    \
NameDef NameDef Number Number
 ("a")  ("b")   (1)    (2)
```

这个流程体现了一个贯穿全节的规律：**`let` 的「左值」是一棵 Pattern 树（可能解构），右值是一棵 `Expr` 树**。两者在 `Let` 节点里汇合。

#### 4.3.3 源码精读

**节点种类枚举**。`AstNodeKind` 给每种节点一个唯一标识，方便序列化与调试：

```cpp
// ast_node.h:35-46（节选）  —— 全部 AST 节点种类
enum class AstNodeKind : uint8_t {
  kArray, kAttr, kAttribute, kBinop, kBuiltinNameDef, kCast,
  kConditional, kConstAssert, kConstFor, kConstantDef, kEnumDef, kFor,
  kFunction, kFunctionRef, kImpl, kImport, kIndex, kInstantiation,
  kInvocation, kLambda, kLet, kMatch, kMatchArm, kModule, kNameDef,
  kNameRef, kNumber, kParam, kParametricBinding, kProc, ...
};
```

参见 [xls/dslx/frontend/ast_node.h:L35-L46](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/frontend/ast_node.h#L35-L46)。本讲涉及的 `kLet`、`kMatch`、`kMatchArm`、`kNameDef`、`kParam`、`kFunction`、`kModule`、`kStatement`、`kStatementBlock` 都在里面。

**节点总表（宏）**。`XLS_DSLX_EXPR_NODE_EACH` 列出所有表达式叶子类型，`ParseLet` 产出的 `Let`、`XlsTuple`、`Number` 都在其中：

```cpp
// ast.h:52-83（节选）  —— 全部 Expr 叶子节点类型
#define XLS_DSLX_EXPR_NODE_EACH(X) \
  X(Array)  X(Attr)  X(Binop)  X(Cast)  X(Conditional)  X(ConstFor) \
  X(For)    X(Index)  X(Invocation)  X(Match)  X(NameRef)  X(Number) \
  X(StatementBlock)  X(StructInstance)  X(TupleIndex)  X(XlsTuple) ...
```

参见 [xls/dslx/frontend/ast.h:L52-L83](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/frontend/ast.h#L52-L83)。注意 `Let`、`MatchArm`、`NameDef`、`Param`、`Function` 等**不是** `Expr`（它们在更大的 `XLS_DSLX_AST_NODE_EACH` 里，见 [xls/dslx/frontend/ast.h:L89-L147](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/frontend/ast.h#L89-L147)）。

**访问者模式**。`AstNodeVisitor` 为每种节点声明一个 `HandleXxx`，遍历 AST 时就不必写一堆 `dynamic_cast`：

```cpp
// ast.h:163-171  —— 双分派访问者：每种节点一个回调
class AstNodeVisitor {
 public:
  virtual ~AstNodeVisitor() = default;
#define DECLARE_HANDLER(__type) \
  virtual absl::Status Handle##__type(const __type* n) = 0;
  XLS_DSLX_AST_NODE_EACH(DECLARE_HANDLER)
#undef DECLARE_HANDLER
};
```

参见 [xls/dslx/frontend/ast.h:L163-L171](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/frontend/ast.h#L163-L171)。每个节点类的 `Accept(v)` 会回调 `v->HandleXxx(this)`，实现「按节点种类分发」。

**核心节点 1：`NameDef`（名字定义点）**。它记录一个标识符及其定义它的节点（`definer`），是作用域解析的目标：

```cpp
// ast.h:1117-1150（节选）  —— 名字定义点
class NameDef : public AstNode {
 public:
  NameDef(Module* owner, Span span, std::string identifier, AstNode* definer);
  AstNodeKind kind() const override { return AstNodeKind::kNameDef; }
  const std::string& identifier() const { return identifier_; }
  void set_definer(AstNode* definer) { definer_ = definer; }
  AstNode* definer() const { return definer_; }
 private:
  Span span_;
  std::string identifier_;
  AstNode* definer_;  // 是哪个节点定义了它（如 Let、Param、Function）
};
```

参见 [xls/dslx/frontend/ast.h:L1117-L1150](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/frontend/ast.h#L1117-L1150)。注意构造函数第一个参数永远是 `Module* owner`——这就是「节点必须由 Module 创建」的体现。

**核心节点 2：`Param`（参数）**。参数 = 名字 + 类型注解：

```cpp
// ast.h:2161-2206（节选）  —— 函数参数：NameDef + TypeAnnotation
class Param : public AstNode {
 public:
  Param(Module* owner, NameDef* name_def, TypeAnnotation* type);
  NameDef* name_def() const { return name_def_; }
  TypeAnnotation* type_annotation() const { return type_annotation_; }
  const std::string& identifier() const { return name_def_->identifier(); }
 private:
  NameDef* name_def_;
  TypeAnnotation* type_annotation_;
  Span span_;
};
```

参见 [xls/dslx/frontend/ast.h:L2161-L2206](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/frontend/ast.h#L2161-L2206)。`a: uN[N]` 这样的参数，`name_def_` 指 `a`，`type_annotation_` 指 `uN[N]`。

**核心节点 3：`Function`（函数）**。它把名字、parametric 绑定、参数列表、返回类型、函数体（`StatementBlock`）打包在一起：

```cpp
// ast.h:2535-2542  —— Function 的字段集合
class Function : public AstNode {
 public:
  Function(Module* owner, Span span, NameDef* name_def,
           std::vector<ParametricBinding*> parametric_bindings,
           std::vector<Param*> params, TypeAnnotation* return_type,
           StatementBlock* body, FunctionTag tag, bool is_public, bool is_stub);
  ...
  StatementBlock* body() const { return body_; }   // 函数体是语句块
  bool IsParametric() const { return !parametric_bindings_.empty(); }
};
```

参见 [xls/dslx/frontend/ast.h:L2535-L2542](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/frontend/ast.h#L2535-L2542) 及成员定义 [xls/dslx/frontend/ast.h:L2659-L2676](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/frontend/ast.h#L2659-L2676)。`return_type_` 可以为空（靠推导），`body_` 永远是 `StatementBlock`。

**核心节点 4：`Statement` 与 `StatementBlock`**。函数体是一个 `StatementBlock`，里面是一串 `Statement`。`Statement` 是个包装器，内部用 `variant` 装四种东西之一——表达式、`Let`、`TypeAlias`、`ConstAssert`：

```cpp
// ast.h:1374-1398（节选）  —— 语句是四种被包装物之一
class Statement final : public AstNode {
 public:
  using Wrapped =
      std::variant<Expr*, TypeAlias*, Let*, ConstAssert*, VerbatimNode*>;
  Statement(Module* owner, Wrapped wrapped);
  const Wrapped& wrapped() const { return wrapped_; }
 private:
  Wrapped wrapped_;
};
```

参见 [xls/dslx/frontend/ast.h:L1374-L1398](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/frontend/ast.h#L1374-L1398)。`StatementBlock` 自身也是一个 `Expr`（块表达式，值为最后一条表达式语句的结果），见 [xls/dslx/frontend/ast.h:L1429-L1468](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/frontend/ast.h#L1429-L1468)。

**核心节点 5：`Let` 与 `Match`/`MatchArm`**。`Let` 持有 Pattern、可选类型注解、右值；`Match` 持有被匹配的表达式和若干 `MatchArm`，每个 `MatchArm` 持有若干 Pattern 和一个结果表达式：

```cpp
// ast.h:4466-4513（节选）  —— Let 节点
class Let : public AstNode {
 public:
  Let(Module* owner, Span span, PatternTree pattern, TypeAnnotation* type,
      Expr* rhs, bool is_const);
  const PatternTree& pattern() const { return pattern_; }
  Expr* rhs() const { return rhs_; }
 private:
  Span span_;
  PatternTree pattern_;       // 左值，可能解构
  TypeAnnotation* type_annotation_;
  Expr* rhs_;
  bool is_const_;
};
```

参见 [xls/dslx/frontend/ast.h:L4466-L4513](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/frontend/ast.h#L4466-L4513)。`Match` 与 `MatchArm` 见 [xls/dslx/frontend/ast.h:L2685-L2761](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/frontend/ast.h#L2685-L2761)：`Match` 的 `matched()` 是被匹配表达式，`arms()` 是分支列表；`MatchArm` 的 `patterns()` 允许 `|` 多模式。

#### 4.3.4 代码实践

**实践目标**：亲手跟踪 `let (a, b) = ...;` 的解析与节点构造，画出 AST 子树。

**操作步骤**：

1. 读 `ParseLet`（[xls/dslx/frontend/parser.cc:L4175-L4245](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/frontend/parser.cc#L4175-L4245)）。注意这几步：
   - 第 4176 行 `PopToken()` 取出 `let`/`const`，据此设 `const_`。
   - 第 4200 行 `PeekTokenIs(kOParen)` 判断「是否解构绑定」。
   - 是的话第 4202 行调 `ParseNameDefPattern`，得到一个 `TuplePattern`。
2. 进入 `ParseNameDefPattern`（[xls/dslx/frontend/parser.cc:L1936-L1972](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/frontend/parser.cc#L1936-L1972)）：它 pop `(`，用 `ParseCommaSeq` 收集逗号分隔的成员（每个成员是 `NameDef` 或更深的 `TuplePattern`），最后 `module_->Make<TuplePattern>(...)` 拼成一个 `TuplePattern`，并检查名字不重复（第 1959-1970 行）。
3. 回到 `ParseLet` 第 4234 行：`module_->Make<Let>(...)` 用 `(pattern, type=null, rhs, const_)` 造出 `Let` 节点。
4. 在纸上画出第 4.3.2 节给出的子树草图，把 `PatternTree` 写成 `TuplePattern(NameDef("a"), NameDef("b"))`。

**需要观察的现象**：`let (a, b) = ...;` 的左值不是 `NameDef`，而是 `TuplePattern`；这正是 `Let::pattern_` 类型为 `PatternTree`（一种 variant，见 [xls/dslx/frontend/ast.h:L201-L203](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/frontend/ast.h#L201-L203)）而非 `NameDef*` 的原因——它要能装下解构。

**预期结果**：你画出的 AST 子树应与 4.3.2 的示意图一致。可对照 `gcd.x` 第 21 行真实的 `let (gcd, _) = for ...`（其中 `_` 是 `WildcardPattern`，见 [xls/dslx/frontend/parser.cc:L4205-L4209](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/frontend/parser.cc#L4205-L4209)）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Let::pattern_` 的类型是 `PatternTree`（一个 variant），而不是直接 `NameDef*`？

> **答案**：因为 `let` 的左值可能是简单名（`NameDef`）、也可能是解构（`TuplePattern`）、通配符（`WildcardPattern`）或数字字面量等。`PatternTree` 是这些可能性的并集，能统一表达「模式」。`NameDef*` 只能表达「单个名字」这一种情况。

**练习 2**：`Statement` 内部用一个 `variant` 装 `Expr*`/`Let*`/`TypeAlias*`/`ConstAssert*`。为什么不直接让这些都继承一个 `Statement` 基类？

> **答案**：因为这些类型本身已有各自的继承归属（`Expr*` 是表达式子类，`Let*` 是独立节点），再硬塞一个 `Statement` 基类会破坏现有的类层次。用 `variant` 包装可以在不改动现有继承关系的前提下，把它们「当作语句」统一放进 `StatementBlock`。这是一种常见的、对已有类型层次侵入更小的设计。

---

### 4.4 Module：顶层编译单元

#### 4.4.1 概念说明

一个 `.x` 文件解析后得到一个 `Module`。`Module` 是 DSLX 的顶层编译单元，它做两件事：

1. **持有顶层成员**：函数、测试、`#[quickcheck]`、结构体、枚举、类型别名、`import`、常量等。这些用一个 `ModuleMember` 的 `variant` 统一表示。
2. **管理全部 AST 节点的内存**：`Module` 内部有一个 `nodes_` 容器（`vector<unique_ptr<AstNode>>`），保存本模块所有节点的所有权。所有 `module_->Make<T>(...)` 创建的节点都进这里，`Module` 析构时统一释放。

> 术语：**编译单元（compilation unit）**指一次编译处理的最顶层单位。在 DSLX 里就是一个模块（一个 `.x` 文件对应一个 `Module`）。

#### 4.4.2 核心流程

`Module` 与解析器、节点之间的关系：

```
Parser 持有一个 Module（owned_module_）
  │
  ├── 解析顶层成员 fn/struct/enum/import/... 
  │     └─> 得到 ModuleMember（variant）
  │           └─> module_->AddTop(member)  存入 top_ 和 top_by_name_
  │
  └── 每次构造节点 module_->Make<T>(args...)
        └─> MakeInternal: new T(this, args...)，存入 nodes_，返回裸指针 T*
              （节点反向持有 owner = this Module，便于再 Make 子节点）
解析完成 -> ParseModule() 返回 unique_ptr<Module>，把整棵树交给调用方
```

关键点：节点之间用**裸指针**互相引用（如 `Function::body_` 指向 `StatementBlock*`），但所有权集中在 `Module::nodes_`。这种「裸指针引用 + 集中所有权」模式，避免了共享指针造成的循环引用和性能开销。

#### 4.4.3 源码精读

**`ModuleMember` 的 variant**。一个顶层成员可能是这些类型中的任意一个：

```cpp
// module.h:49-53  —— 顶层成员的并集类型
using ModuleMember =
    std::variant<Function*, Proc*, TestFunction*, TestProc*, QuickCheck*,
                 TypeAlias*, StructDef*, ProcAlias*, ProcDef*, ConstantDef*,
                 EnumDef*, Import*, Use*, ConstAssert*, Impl*, Trait*,
                 VerbatimNode*, FuzzTestFunction*>;
```

参见 [xls/dslx/frontend/module.h:L49-L53](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/frontend/module.h#L49-L53)。本系列前面讲过的 `#[test]`（`TestFunction`）、`#[quickcheck]`（`QuickCheck`）、`import std;`（`Import`）都是这里的成员类型。

**`Module` 类与顶层成员访问**。`top()` 返回成员序列，`GetFunction`/`GetTest` 等是按名查找的便捷方法：

```cpp
// module.h:112-132（节选）  —— Module 作为顶层编译单元
class Module : public AstNode {
 public:
  Module(std::string name, std::optional<std::filesystem::path> fs_path,
         FileTable& file_table);
  AstNodeKind kind() const override { return AstNodeKind::kModule; }
  absl::Span<ModuleMember const> top() const { return top_; }
  std::optional<Function*> GetFunction(std::string_view target_name) const {
    return GetMember<Function>(target_name);
  }
  ...
};
```

参见 [xls/dslx/frontend/module.h:L112-L132](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/frontend/module.h#L112-L132)。`Module` 本身也是一个 `AstNode`（`kModule`），所以它是整棵 AST 的根。

**节点工厂 `Make`**。所有节点（`BuiltinNameDef` 除外）都必须通过 `Make` 创建，它转发到私有的 `MakeInternal`：

```cpp
// module.h:148-153  —— 节点工厂（公开模板）
template <typename T, typename... Args>
T* Make(Args&&... args) {
  static_assert(!std::is_same<T, BuiltinNameDef>::value,
                "Use Module::GetOrCreateBuiltinNameDef()");
  return MakeInternal<T, Args...>(std::forward<Args>(args)...);
}
```

```cpp
// module.h:401-409  —— 真正分配节点并集中持有所有权
template <typename T, typename... Args>
T* MakeInternal(Args&&... args) {
  std::unique_ptr<T> node =
      std::make_unique<T>(this, std::forward<Args>(args)...);
  T* ptr = node.get();
  ptr->SetParentage();           // 回填 parent 指针，便于向上遍历
  nodes_.push_back(std::move(node));   // 所有权交给 Module
  return ptr;
}
```

参见 [xls/dslx/frontend/module.h:L148-L153](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/frontend/module.h#L148-L153) 与 [xls/dslx/frontend/module.h:L401-L409](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/frontend/module.h#L401-L409)。注意 `std::make_unique<T>(this, ...)`——节点构造时第一个实参永远是 `this Module*`，所以每个节点都「知道」自己属于哪个模块，反过来还能再调 `module_->Make` 造子节点。`SetParentage()` 会把子节点的 `parent` 指针设好，方便从任意子节点向上找到根。

**添加顶层成员 `AddTop`**。`ParseModule` 解析出一个顶层成员后，调 `AddTop` 把它挂到模块上，并做命名冲突检查：

```cpp
// module.h:179-180  —— 加入顶层成员（带冲突检测回调）
absl::Status AddTop(ModuleMember member,
                    const MakeCollisionError& make_collision_error);
```

参见 [xls/dslx/frontend/module.h:L179-L180](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/frontend/module.h#L179-L180)。冲突检测的回调由解析器注入（`MakeModuleTopCollisionError`），这样错误信息能带上源码位置。

**成员存储的两个数据结构**。`Module` 同时维护一个有序向量 `top_` 和一张按名映射表 `top_by_name_`：

```cpp
// module.h:453-460（节选）  —— 顶层成员的两种索引
std::vector<ModuleMember> top_;                 // 保序，用于按定义顺序遍历
absl::flat_hash_set<const AstNode*> top_set_;   // 快速 contains 判断
absl::flat_hash_map<std::string, ModuleMember> top_by_name_;  // 按名查找
```

参见 [xls/dslx/frontend/module.h:L453-L460](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/frontend/module.h#L453-L460)。向量保序（`GetFunctionNames()` 返回的就是定义顺序），哈希表加速按名查找（`GetFunction("gcd")`）。这是典型的「空间换时间」冗余索引。

#### 4.4.4 代码实践

**实践目标**：验证 `Module` 的「工厂 + 集中所有权」模型，并理解顶层成员的两条索引。

**操作步骤**：

1. 打开 `ParseModule` 顶层循环（[xls/dslx/frontend/parser.cc:L449-L558](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/frontend/parser.cc#L449-L558)）。找到解析 `fn` 后调用 `module_->AddTop(...)` 的位置（在该函数后半段，对各种关键字分派后）。
2. 打开 `ParseFunctionInternal`（[xls/dslx/frontend/parser.cc:L2507-L2558](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/frontend/parser.cc#L2507-L2558)），确认函数体是 `ParseBlockExpression` 得到的 `StatementBlock*`，且函数名在签名解析完之后才加入 `bindings`（第 2539-2540 行）——这阻止了函数在自身签名里自引用。
3. 在 `module.h` 里读 `GetFunction`（[xls/dslx/frontend/module.h:L217-L219](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/frontend/module.h#L217-L219)），它内部走 `GetMember<Function>`，后者查的是 `top_by_name_`（[xls/dslx/frontend/module.h:L184-L190](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/frontend/module.h#L184-L190)）。
4. 回答：`Module` 既要 `top_`（向量）又要 `top_by_name_`（哈希表），各自服务什么用途？

**需要观察的现象**：`Make` 返回的是裸指针 `T*`，调用方不持有 `unique_ptr`；所有 `unique_ptr` 都在 `Module::nodes_` 里。这说明节点内存的生命周期与 `Module` 完全一致。

**预期结果**：你能说清——`top_` 维持定义顺序、支持顺序遍历（列出所有函数名）；`top_by_name_` 支持按名 O(1) 查找（`GetFunction`）。

#### 4.4.5 小练习与答案

**练习 1**：为什么节点之间用裸指针互相引用，而不直接用 `shared_ptr`？

> **答案**：AST 是树形结构，所有权天然单一（由 `Module` 集中持有）。用 `shared_ptr` 会引入引用计数开销，且容易因环形引用（如子节点回指 `Module`/`parent`）造成内存泄漏。裸指针引用 + 集中 `unique_ptr` 所有权，既简单又安全：只要 `Module` 活着，所有节点都有效；`Module` 一析构，全部节点一起释放。

**练习 2**：`ParseFunctionInternal` 为什么要等函数签名（参数、返回类型）解析完，才把函数名加入 `bindings`？

> **答案**：为了让函数在自己的签名里**不能**引用自身。如果一开头就把名字加入作用域，那么返回类型或 parametric 默认值里就能引用这个尚未定义完的函数，容易造成循环定义。延后加入可以从语法层面杜绝这种自引用（见源码注释 [xls/dslx/frontend/parser.cc:L2511-L2513](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/frontend/parser.cc#L2511-L2513) 与 [xls/dslx/frontend/parser.cc:L2536-L2540](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/frontend/parser.cc#L2536-L2540)）。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「端到端 AST 追踪」小任务。

**任务**：给定 `gcd.x` 里的这一句（第 21 行，略作简化）：

```dslx
let (gcd, _) = for (_, (a, b)) in u32:0..DN { ... }((a, b));
```

按下面的链路，把每一步对应的源码函数和 AST 节点都标出来：

1. **扫描**：确认 `let` → `kKeyword(kLet)`、`(` → `kOParen`、`gcd` → `kIdentifier`、`_` → `kIdentifier`（值为 `"_"`）。引用 `scanner_keywords.inc` 与 `token.h`。
2. **分派**：在 `ParseBlockExpression`（[xls/dslx/frontend/parser.cc:L4710-L4713](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/frontend/parser.cc#L4710-L4713)）里，`let` 关键字触发了对 `ParseLet` 的调用。
3. **解构左值**：`ParseLet` 看到 `(`，调用 `ParseNameDefPattern`（[xls/dslx/frontend/parser.cc:L4200-L4202](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/frontend/parser.cc#L4200-L4202)），产出 `TuplePattern(NameDef("gcd"), WildcardPattern)`。注意 `_` 被特判为 `WildcardPattern`（[xls/dslx/frontend/parser.cc:L4205-L4209](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/frontend/parser.cc#L4205-L4209)）。
4. **右值**：`ParseExpression` 看到 `for`，路由到 `ParseFor`（[xls/dslx/frontend/parser.cc:L1357-L1359](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/frontend/parser.cc#L1357-L1359)），产出 `For` 节点。
5. **组装**：`module_->Make<Let>(...)` 把左值 Pattern 和右值 `For` 拼成 `Let`（[xls/dslx/frontend/parser.cc:L4234](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/frontend/parser.cc#L4234)），再用 `module_->Make<Statement>(let)` 包成语句塞进函数体的 `StatementBlock`。
6. **挂到模块**：整个函数 `gcd_euclidean` 作为一个 `Function*`，经 `module_->AddTop` 存入 `Module::top_`（参见 [xls/dslx/frontend/module.h:L179-L180](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/frontend/module.h#L179-L180)）。

**交付物**：画一张完整的 AST 子树（从 `Function` 往下，到这条 `let` 的 `TuplePattern`、`WildcardPattern`、`For`），并在每个节点旁标注它的 C++ 类名（如 `Let`、`TuplePattern`、`NameDef`、`WildcardPattern`、`For`）和 `AstNodeKind` 枚举值。

**预期结果**：你能从一句 DSLX 文本，逐层还原到「扫描器 Token → 解析器函数调用栈 → AST 节点树 → Module 持有」的全链路。这就达成了本讲的全部目标。运行结果相关部分可标注「待本地验证」（例如用调试器在 `ParseLet` 处下断点观察实际产出的节点）。

## 6. 本讲小结

- DSLX 前端分两步：**`Scanner` 把文本切成 Token 流**，**`Parser` 用递归下降把 Token 组织成 AST**。Token 的「字母表」由 `token.h` 与 `scanner_keywords.inc` 的宏定义，`u8`/`u32` 等是关键字而非普通标识符。
- `Parser` 继承 `TokenParser`，靠「peek 分派 + drop 消费」驱动；`Bindings` 以带 `parent` 的链表实现词法作用域，`ExpressionDepthGuard` 用 RAII 防止表达式嵌套过深。
- AST 节点统一继承 `AstNode`，分「表达式（`Expr`）」与非表达式两类，全部叶子类型由 `XLS_DSLX_AST_NODE_EACH` 宏罗列；遍历用访问者模式 `AstNodeVisitor`。核心节点包括 `Function`/`Param`/`NameDef`/`Statement`/`StatementBlock`/`Let`/`Match`/`MatchArm`。
- **所有节点都必须经 `Module::Make` 创建**，所有权集中在 `Module::nodes_`，节点之间用裸指针互引——这是「集中所有权 + 裸引用」的典型内存模型。
- `Module` 是顶层编译单元，用 `ModuleMember` variant 持有函数/测试/结构体/import 等顶层成员，并用 `top_`（保序向量）与 `top_by_name_`（按名哈希）两套索引兼顾遍历与查找。
- `let (a, b) = ...;` 的左值是 `PatternTree`（含 `TuplePattern`），右值是 `Expr`，二者在 `Let` 节点汇合——这是理解 DSLX 解析的一把钥匙。

## 7. 下一步学习建议

本讲结束时，你拿到了一棵**没有类型信息**的 AST。下一步自然是给每个节点补上类型：

- **下一讲 u2-l3「DSLX 类型推导与检查」**：阅读 `xls/dslx/type_system/` 下的 `type.h`、`type_info.h`、`parametric_env.h`，看类型系统如何遍历这棵 AST、为每个表达式节点关联类型，以及 parametric 函数（如 `gcd.x` 里的 `<N: u32>`）如何被实例化。
- **后续 u3-l4「从 DSLX 到 IR 的转换」**：看 `xls/dslx/ir_convert/function_converter.h` 如何把这棵带类型的 AST 进一步 lowering 成 XLS IR 的数据流图——届时本讲的 `Function`、`Let`、`Match`、`For` 节点会一个个被翻译成 IR 的 `Node`。
- **想加深理解本讲**：可以挑一个尚未细读的解析函数（如 `ParseFor` 在 [xls/dslx/frontend/parser.cc:L4247](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/frontend/parser.cc#L4247)、`ParseMatch` 在 [xls/dslx/frontend/parser.cc:L2315](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/frontend/parser.cc#L2315)），用本讲的方法（peek 分派 → drop 起始关键字 → 递归子结构 → `module_->Make` 组装）自行跟踪一遍它如何构造对应的 `For` / `Match` 节点。
