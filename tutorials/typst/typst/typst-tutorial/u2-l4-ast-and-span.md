# AST 视图与 Span 定位系统

> 所属单元：u2 语法层：typst-syntax 的解析世界
> 前置讲义：u2-l3（语法树：Parser 与 SyntaxNode）

## 1. 本讲目标

上一讲我们看到，Parser 产出一棵「永不失败」的无类型具体语法树（CST），节点是 SyntaxNode，只知道自己的 SyntaxKind。本讲回答两个问题：

1. 求值器（typst-eval）如何安全、零成本地从这棵无类型树上读出「语义」——比如一个函数调用的被调者和参数列表？答案是 **ast.rs 的类型化 AST 视图**。
2. 树上的每个节点如何与源码文本位置对应起来，让报错能画波浪线、编辑器能跳转？答案是 **span.rs 的编号式 Span 定位系统**。

学完本讲你将能够：

- 说出「一棵树、两个视图」的设计：AST 不是第二棵树，而是包在 SyntaxNode 引用上的薄包装。
- 使用 SyntaxNode::cast 把节点转换成 FuncCall、FieldAccess、Args 等类型化视图，并调用其语义访问器。
- 理解 Span 的 8 字节打包布局、编号有序性，以及它为什么对增量编译和缓存友好。
- 走通「节点 → Span → 源码字节区间」的正反向链路：SyntaxNode::span、Source::find、Source::range、WorldExt::range。
- 亲手遍历一段代码的 AST，收集所有函数调用的函数名与 Span，再反查打印出对应源码片段。

> **本次更新说明（对应 HEAD 84cca406）**：本讲涉及的 API 有两处变化。其一，`Span` 与 `DiagSpan` 不再派生 `Debug`，改为手写实现，调试输出从原始位模式变成直接展示解包后的 `SpanKind`/`DiagSpanKind`，打印 span 时终于「可读」了；其二，测试辅助构造器 `Source::detached` 被移除，`Source::new` 的文本参数放宽为 `impl Into<String>`，取而代之的新入口是 `FileId::new_for_test`（path.rs 新增）。本讲正文与全部示例已同步迁移。

## 2. 前置知识

- **CST（具体语法树）**：u2-l3 讲过，SyntaxNode 是无损的，中序遍历能逐字节还原源码，空白、注释等 trivia 也在树上。但它只告诉你「这是什么种类（SyntaxKind）」，不告诉你「语义结构是什么」。
- **视图（view）**：不复制数据、不另建一棵树，而是用一层薄薄的 Rust 类型包住已有数据的引用，按需提供更有语义的方法。类比：同一份数据库表，可以给不同用户开不同的「视图」。
- **Rust 预备知识**：
  - **trait 与默认方法**：trait 可以给出方法默认实现，实现者可直接继承（本讲的 AstNode::span 就是默认方法）。
  - **泛型单态化**：cast::<T> 在编译期为每个 T 生成专用代码，运行时没有虚表开销。
  - **生命周期 'a**：AST 包装持有 &'a SyntaxNode，保证视图活得不比底层节点久。
  - **repr(transparent)**：单字段包装结构体，内存布局与内部类型一致——「零成本」的来源。
  - **macro_rules! 声明宏**：按模式批量生成重复代码，本讲的 node! 宏靠它批量生产包装类型。
- **二进制位运算**：Span 用移位和按位或把「文件编号 + 节点编号」打包进一个 64 位整数，需要一点点移位运算的直觉。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| crates/typst-syntax/src/ast.rs | 约 2800 行的 AST 视图层 | AstNode trait、cast、node! 宏、Expr 枚举、FuncCall/FieldAccess/Args |
| crates/typst-syntax/src/span.rs | Span/DiagSpan/SpanKind/SubRange | 8 字节位布局、编号语义、有序性保证，以及新的手写 Debug 输出 |
| crates/typst-syntax/src/set.rs | SyntaxSet：SyntaxKind 的位集合 | 解析器做「什么能开始一个表达式」决策的工具（与 Span 无直接关系，本讲纠正一个常见误解） |
| crates/typst-syntax/src/node.rs | CST 节点本体（u2-l3 主角） | span() 访问器、children()、numberize、LinkedNode |
| crates/typst-syntax/src/path.rs | FileId 与虚拟路径（u2-l1 主角） | FileId::new_for_test：本讲所有示例构造 Source 的测试 id 来源（本次新增的测试辅助） |
| crates/typst-syntax/src/source.rs | 源文件抽象（u2-l1 主角） | Source::new（签名已放宽为 impl Into<String>）中的 numberize、Source::find、Source::range |
| crates/typst-library/src/lib.rs | 编译器库总入口 | WorldExt::range：Span 到字节区间的官方入口 |
| crates/typst-eval/src/code.rs、call.rs | 求值器（下一单元主角） | AST 视图的「消费方」，验证这套设计真的被这样使用 |

## 4. 核心概念与源码讲解

### 4.1 AST 类型化视图：一棵树、两个视图

#### 4.1.1 概念说明

ast.rs 的模块文档开宗明义：Typst 的 AST 是「对无类型 CST 的一个惰性、类型化视图」。它有三层含义：

1. **AST 是视图**：绝大多数 AST 节点就是 struct Xxx<'a>(&'a SyntaxNode) 这样的单字段包装——不复制树、不重排结构，抽象掉 CST 里的定界符等噪音。文档以 Raw 为例：CST 里有成对的 RawDelim 反引号节点，AST 则把这些定界符「抽象」掉，只暴露 lines()、lang()、block() 这些语义方法。
2. **AST 是类型化的**：每个 AST 类型实现 AstNode trait，并提供自己独有的语义方法。类型系统保证你只有先「验明正身」（kind 匹配）才能拿到包装，因此调用语义方法时不可能张冠李戴。
3. **AST 以 Markup 为根**：Markup::exprs 返回统一的 Expr 枚举迭代器，markup、math、code 三种模式的表达式都汇聚在 Expr 里。

「一棵树、两个视图」指的就是：CST（SyntaxNode）是唯一存储；AST（ast.rs 的包装类型）是按需借出的类型化透镜。同一个 SyntaxNode 引用可以先 cast 成 FuncCall 读被调者，再 cast 成别的视图，互不干扰。

#### 4.1.2 核心流程

从源码字符串到语义访问的完整链路：

```text
parse(text)            产出无类型 CST（SyntaxNode，kind 已定，span 尚未编号）
   |
Source::new(id, text)  内部调用 root.numberize(...) 给树上每个节点编 span 号
   |
source.root()          拿到根节点引用 &SyntaxNode
   |
node.cast::<T>()       泛型转换入口，转发 T::from_untyped(node)
   |                   按 kind 匹配：匹配成功返回包装，失败返回 None
   v
FuncCall<'a> 视图      调用语义方法：callee() / args() / span()
```

关键设计决策：

- **转换可能失败，但永不 panic**：cast 返回 Option，由调用方决定匹配失败怎么办（跳过、报错或使用占位符）。
- **容错树的兜底**：树上有错误节点时，语义方法找不到期望的孩子怎么办？cast_first/cast_last 会退回 T::placeholder()——一个静态的、同 kind 的占位节点，让求值器「拿到一个无效但存在的节点」继续走错误恢复路径，而不是崩溃。
- **kind 检查即安全边界**：node! 宏生成的 from_untyped 只比较 node.kind() == SyntaxKind::Xxx，匹配就意味着「这是解析器亲手产出的该结构」，其内部孩子布局可信。

#### 4.1.3 源码精读

**① AstNode trait：类型化视图的契约**

[crates/typst-syntax/src/ast.rs:L99-L117](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/ast.rs#L99-L117) 定义了所有 AST 节点的公共接口：from_untyped 尝试把无类型节点转成视图、to_untyped 借回底层节点引用，span() 是默认方法——直接委托给底层节点的 span，placeholder() 提供容错兜底。

```rust
pub trait AstNode<'a>: Sized {
    /// Convert a node into its typed variant.
    fn from_untyped(node: &'a SyntaxNode) -> Option<Self>;

    /// A reference to the underlying syntax node.
    fn to_untyped(self) -> &'a SyntaxNode;

    /// The source code location.
    fn span(self) -> Span {
        self.to_untyped().span()
    }

    /// A placeholder for this node type. ...
    fn placeholder() -> Self;
}
```

**② cast / is：挂在 SyntaxNode 上的通用转换接口**

[crates/typst-syntax/src/ast.rs:L120-L129](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/ast.rs#L120-L129) 给 SyntaxNode 加了两个泛型方法：is::<T>() 问「能不能转」，cast::<T>() 真正去转，全部工作只是转发 T::from_untyped(self)：

```rust
impl SyntaxNode {
    /// Whether the node can be cast to the given AST node.
    pub fn is<'a, T: AstNode<'a>>(&'a self) -> bool {
        self.cast::<T>().is_some()
    }

    /// Try to convert the node to a typed AST node.
    pub fn cast<'a, T: AstNode<'a>>(&'a self) -> Option<T> {
        T::from_untyped(self)
    }
```

紧接着的 [crates/typst-syntax/src/ast.rs:L131-L149](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/ast.rs#L131-L149) 是四个 crate 内部辅助：try_cast_first/try_cast_last 在孩子里找第一个/最后一个可转换节点（find_map，天然跳过 trivia 和不认识的 kind），cast_first/cast_last 找不到时用 T::placeholder() 兜底——语义访问器不 panic 的秘密就在这里。

**③ node! 宏：批量生产包装类型**

[crates/typst-syntax/src/ast.rs:L165-L196](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/ast.rs#L165-L196) 的声明宏为每个「与 SyntaxKind 同名」的结构生成全套实现。注意 repr(transparent)——这就是零成本包装：

```rust
macro_rules! node {
    ($(#[$attr:meta])* struct $name:ident) => {
        #[derive(Debug, Copy, Clone, Eq, PartialEq, Hash)]
        #[repr(transparent)]
        $(#[$attr])*
        pub struct $name<'a>(&'a SyntaxNode);

        impl<'a> AstNode<'a> for $name<'a> {
            #[inline]
            fn from_untyped(node: &'a SyntaxNode) -> Option<Self> {
                if node.kind() == SyntaxKind::$name {
                    Some(Self(node))
                } else {
                    Option::None
                }
            }
            // to_untyped 直接返回 self.0（L183-L186）
            // placeholder 用 static PLACEHOLDER 兜底（L188-L193）
        }
    };
}
```

之后每个 AST 类型只需一行声明，例如 [crates/typst-syntax/src/ast.rs:L198-L205](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/ast.rs#L198-L205) 声明 LineComment 并提供 text() 方法。全文有 70 多处 node! 调用（Markup、Text、Heading、Ident、FieldAccess、FuncCall、Args、Closure 等，见文件内 L226、L608、L845、L1396、L2121、L2138、L2155、L2210 等处）。

**④ 以函数调用为例：三个视图如何协作**

[crates/typst-syntax/src/ast.rs:L2119-L2151](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/ast.rs#L2119-L2151) 定义 FieldAccess 与 FuncCall 的语义方法——全部用 cast_first/cast_last 在孩子里「捞」出需要的视图：

```rust
impl<'a> FieldAccess<'a> {
    /// The expression to access the field on.
    pub fn target(self) -> Expr<'a> { self.0.cast_first() }

    /// The name of the field.
    pub fn field(self) -> Ident<'a> { self.0.cast_last() }
}

impl<'a> FuncCall<'a> {
    /// The function to call.
    pub fn callee(self) -> Expr<'a> { self.0.cast_first() }

    /// The arguments to the function.
    pub fn args(self) -> Args<'a> { self.0.cast_last() }
}
```

对 f(x, y) 这棵 CST 子树：第一个能转成 Expr 的孩子是 f（被调者），最后一个是 Args 节点，中间的括号等 token 自然被跳过。

[crates/typst-syntax/src/ast.rs:L2153-L2206](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/ast.rs#L2153-L2206) 是参数列表：Args::items 用 children().filter_map(SyntaxNode::cast) 把每个孩子转成 Arg（逗号被 cast 拒绝而过滤）；Arg 是手写的枚举——Named（draw: false）、Spread（..things）各自对应 kind，其余统统转成 Expr 作为位置参数：

```rust
pub enum Arg<'a> {
    Pos(Expr<'a>),      // 位置参数
    Named(Named<'a>),   // 命名参数
    Spread(Spread<'a>), // 展开参数
}

impl<'a> AstNode<'a> for Arg<'a> {
    fn from_untyped(node: &'a SyntaxNode) -> Option<Self> {
        match node.kind() {
            SyntaxKind::Named => Some(Self::Named(Named(node))),
            SyntaxKind::Spread => Some(Self::Spread(Spread(node))),
            _ => node.cast().map(Self::Pos),
        }
    }
}
```

**⑤ 根视图与统一表达式枚举**

[crates/typst-syntax/src/ast.rs:L226-L249](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/ast.rs#L226-L249)：AST 的根 Markup 只有一个方法 exprs()，它一边过滤「无分号语句后面的换行」（L234-L247），一边用 cast_with_space 转换（空格默认会被普通 cast 跳过，见 L248）。

[crates/typst-syntax/src/ast.rs:L254-L312](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/ast.rs#L254-L312) 的 Expr<'a> 枚举把 markup、math、code 三种模式的表达式统一收编（Text、Strong、Heading、MathFrac、FuncCall、Binary 等共 70 多个变体）。它的 from_untyped 是一张巨大的 kind 到变体对照表，见 [crates/typst-syntax/src/ast.rs:L403-L407](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/ast.rs#L403-L407)，注意注释「Spaces are skipped unless using cast_with_space」。Ident 的取文本方法在 [crates/typst-syntax/src/ast.rs:L1396-L1411](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/ast.rs#L1396-L1411)（get() 返回 EcoString，as_str() 返回字符串切片，还 Deref 到 str）。

**⑥ 消费方验证：typst-eval 就是这么用的**

这套视图不是摆设。求值器对 Expr 的大 match 里，几乎每个分支都是「取出视图、调语义方法」：[crates/typst-eval/src/code.rs:L133-L134](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-eval/src/code.rs#L133-L134) 是 FieldAccess/FuncCall 分支；函数调用的求值入口在 [crates/typst-eval/src/call.rs:L24](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-eval/src/call.rs#L24)（impl Eval for ast::FuncCall<'_>），其中 [crates/typst-eval/src/call.rs:L34](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-eval/src/call.rs#L34) 用 if let ast::Expr::FieldAccess(access) = callee 判断「这是不是方法调用」。第三单元会精读这些代码，这里只需确认设计被如实使用。

**⑦ 补充：set.rs 是 SyntaxKind 的集合工具，与 Span 无关**

一个常见误解是「set.rs 和 Span 有关」。读源码会发现并非如此：[crates/typst-syntax/src/set.rs:L7-L48](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/set.rs#L7-L48) 的 SyntaxSet 是一个 u128 位图（致敬 rust-analyzer 的 TokenSet），new/add/remove/union/contains 全是 const fn。它真正服务的是解析器：STMT（L63）声明哪些 kind 能开始一条语句，CODE_EXPR（L96-L97）声明哪些 kind 能开始一个代码表达式，解析器用 [crates/typst-syntax/src/parser.rs:L1641-L1642](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/parser.rs#L1641-L1642) 的 at_set 做超前判断。它与本讲的间接联系是：**SyntaxSet 决定解析器在每种模式下认哪些结构，进而决定 CST 上会出现哪些 kind，进而决定 cast 能成功哪些 AST 视图**。这是「解析决策、树、视图」的一条因果链，而非 Span 相关机制。

#### 4.1.4 代码实践

**实践目标**：直观感受「cast 是按 kind 过滤的」——给一段文档，打印每个顶层 Expr 变体的种类，并观察空白被折叠。

1. 在仓库根目录新建 crates/typst-syntax/examples/kinds.rs（示例代码，练习后删除）：

```rust
use typst_syntax::{FileId, Source, ast::{self, AstNode}};

fn main() {
    // 旧版这里是 Source::detached(...)；现在用 FileId::new_for_test 构造测试 id
    let id = FileId::new_for_test("kinds");
    let source = Source::new(id, "= 标题\nsome *bold* text\n#calc.max(1, 2)");
    let markup = source.root().cast::<ast::Markup>().unwrap();
    for expr in markup.exprs() {
        println!("{:?}", expr.to_untyped().kind());
    }
}
```

2. 运行：cargo run -p typst-syntax --example kinds
3. 观察现象：输出应依次出现 Heading、Space、Text、Strong、Space、Text，以及最后的 FuncCall。
4. 预期结果：exprs() 产出的都是「有意义」的表达式；空白折叠成 Space 变体而非消失（cast_with_space 的功劳）；井号进入代码模式后产出的 calc.max(1, 2) 是一个 FuncCall 节点。具体输出待本地验证。
5. 删除 crates/typst-syntax/examples/kinds.rs（若目录为空可一并删除）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 FuncCall::callee 用 cast_first 而 args 用 cast_last？对 f(x)(y)（返回值再调用）这样的嵌套调用，这个设计还能工作吗？

答案：FuncCall 的 CST 结构是「被调者表达式 + Args」两段，第一个可转成 Expr 的孩子必是被调者，最后一个是本次调用的参数表。对 f(x)(y)，外层 FuncCall 的孩子是「内层 FuncCall（被调者）+ Args(y)」：cast_first 捞到内层调用作为 callee，cast_last 捞到 (y) 作为 args，仍然正确——视图方法对嵌套结构天然成立。

**练习 2**：Markup::exprs 返回的迭代器为什么能跳过注释和逗号这类 token？

答案：exprs 内部（L233-L249）用 filter_map(Expr::cast_with_space) 转换每个孩子；cast_with_space 对不认识的 kind（注释、标点、定界符）统一返回 None，被 filter_map 静默丢弃。视图转换本身就是过滤器。

**练习 3**：cast 与 placeholder 的分工是什么？

答案：cast 负责「验证」——kind 不匹配返回 None，由调用方决定跳过还是报错；placeholder 负责「兜底」——当树因错误而不完整、cast_first/cast_last 找不到孩子时，返回一个静态占位节点，让后续代码拿到「格式合法但内容空洞」的视图继续错误恢复，避免 panic。这是 u2-l3「解析永不失败」哲学在 AST 层的延续。

### 4.2 Span 与节点关联：编号式定位系统

#### 4.2.1 概念说明

有了类型化视图，还差「位置」。直觉方案是让每个节点直接存字节区间，但它有个致命弱点：**你在文档中间敲一个字符，后面所有节点的区间都要平移**。对增量编译（comemo 按输入记忆化，见 u1-l3、u5-l8）来说，输入变了缓存就失效——频繁编辑会让缓存形同虚设。

Typst 的答案是**编号式 Span**（span.rs 文档 L42-L50 详细论证）：

- 每个节点在树被 Source 接管时获得一个**区间内唯一的编号**，编辑时编号尽量保持稳定（插入点之后的节点编号基本不动）。
- Span 只有 8 字节，且因 NonZeroU64 的 null 优化，Option<Span> 也是 8 字节。
- 编号不是随机分配，而是**有序**的：父节点编号小于全部子孙，兄弟从左到右递增——这让「按编号找节点」可以像二分查找一样剪枝。

#### 4.2.2 核心流程

**编号何时发生**：parse 只建树不编号；Source::new 在建树后调用 root.numberize(id, Span::FULL)（source.rs L39）给全树分配编号。增量重解析时 reparser 只对受影响区间重新编号（u2-l5 详述）。

**8 字节位布局**（span.rs L92-L101 的注释原文给出）：

\[ \text{Span} = (\text{file\_id} \ll 48) \;|\; \text{low48} \]

其中高 16 位是文件编号 FileId，低 48 位按用途划分：

- 编号 1 保留给「detached」（不属于任何文件）；
- Typst 源码树节点编号：\( 2 \le n < 2^{47} \)（即 Span::FULL，L87）；
- 外部文件诊断起点：\( 2^{47} \le n < 2^{47} + 2^{46} \)；
- 内部区间 Span：\( 2^{47} + 2^{46} \le n < 2^{48} \)，此时低 48 位再拆成两个 23 位存区间的 start/end（from_range，L128-L133：number = (start << 23) | end）。

**有序性如何被利用**（find_number 的剪枝，node.rs L1024-L1055）：若目标编号小于当前节点编号，才需要在孩子里继续找；又因兄弟编号从左到右递增，遍历孩子时一旦发现下一个兄弟的编号已超过目标，即可跳过其后所有子树。整体近似沿树高下行的小规模搜索，而非全树遍历。

#### 4.2.3 源码精读

**① Span 的文档与结构**

[crates/typst-syntax/src/span.rs:L9-L61](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/span.rs#L9-L61) 的文档非常值得一读：它说明了 Span 的用途（追踪元素来源与诊断位置）、WorldExt::range 的存在（L18-L19）、四种内部形态（detached、编号、原始区间、外部文件起点）以及编号式设计的动机——「编辑时编号基本稳定，可作为记忆化函数的输入而不伤缓存」（L43-L50）。结构本体只有一行（L62-L63）：

```rust
#[derive(Copy, Clone, Eq, PartialEq, Hash)]
pub struct Span(NonZeroU64);
```

注意 derive 列表里已经没有 `Debug`——本次更新改为手写实现（[span.rs:L207-L212](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/span.rs#L207-L212)）：

```rust
impl Debug for Span {
    fn fmt(&self, f: &mut Formatter<'_>) -> fmt::Result {
        // Output the `SpanKind` for more useful debugging.
        f.debug_tuple("Span").field(&self.get()).finish()
    }
}
```

以前 `println!("{:?}", span)` 打出来的是一串不知所云的 64 位整数；现在先经 `self.get()` 解包再输出，形如 `Span(Number { id: FileId(..), num: SpanNumber(2) })`。对调试器和日志来说这是个实打实的体验改进，本讲后面打印 span 时会直接受益。

[crates/typst-syntax/src/span.rs:L65-L83](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/span.rs#L65-L83) 是 SpanNumber（编号的公开类型）与 SpanKind 枚举（Detached、Number、Range）——把压缩表示展开成好用的形式。

**② 位布局与打包/解包**

[crates/typst-syntax/src/span.rs:L85-L110](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/span.rs#L85-L110) 定义全部常量：FULL = 2..(1 << 47)、NUMBER_BITS = 48、FILE_ID_SHIFT = 48、RANGE_BASE 等，注释里画出了位布局图。

[crates/typst-syntax/src/span.rs:L144-L150](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/span.rs#L144-L150) 的 pack 把文件 id 和低 48 位拼起来：

```rust
const fn pack(id: FileId, low: u64) -> Self {
    let bits = ((id.into_raw().get() as u64) << Self::FILE_ID_SHIFT) | low;
    // The file ID is non-zero.
    Self(NonZeroU64::new(bits).unwrap())
}
```

[crates/typst-syntax/src/span.rs:L177-L187](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/span.rs#L177-L187) 的 get() 是解包入口：先看文件 id 是否为零（detached），再用 checked_sub(RANGE_BASE) 判断低 48 位是不是「区间 Span」，否则就是普通编号 Span。id()（L160-L167）与 number()（L170-L172）分别取高低两半。

**③ 节点侧：span 存在哪**

每个 SyntaxNode 内部就存着自己的 span，[crates/typst-syntax/src/node.rs:L242-L245](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/node.rs#L242-L245) 只是一个字段读取：

```rust
/// The span of the node.
pub fn span(&self) -> Span {
    self.span
}
```

编号入口在 [crates/typst-syntax/src/node.rs:L528-L543](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/node.rs#L528-L543)：SyntaxNode::numberize 是个 pub(super) 包装——叶子节点直接取可用区间的中点，内部节点则转交给 [node.rs:L679-L708](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/node.rs#L679-L708) 的 InnerNode::numberize 递归编号。注意递归本体的签名本次更新后多了第一个参数 `span: Option<&mut Span>`——调用方把节点自身的 span 槽借进来，让编号函数能顺手把结果写回去；这是 u2-l5 增量重解析重构的一环（「编号只占可用区间左半、留出空隙供增量插入」的策略 u2-l3 已讲过）。触发点在 [crates/typst-syntax/src/source.rs:L31-L41](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/source.rs#L31-L41)：Source::new 建树后立刻 root.numberize(id, Span::FULL)（L39）。

另一个 API 变化要特别留意：**旧版测试常用的 `Source::detached` 已被移除**。现在 `Source::new` 的文本参数放宽为 `impl Into<String>`（&str 可以直接传入），构造测试 id 用 [crates/typst-syntax/src/path.rs:L171-L177](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/path.rs#L171-L177) 新增的 `FileId::new_for_test("测试名")`——它以测试名为虚拟路径在 Project 根下驻留一个 FileId，本质是旧 detached 内部做法的公开化。仓库自己的测试就是这么迁移的：source.rs 的 test_source_sub_ranges（[source.rs:L154-L158](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/source.rs#L154-L158)）和 node.rs 的 test_linked_node 都改用了它。

**④ DiagSpan：诊断专用的加强版**

诊断（错误/警告）有时要指向非 Typst 文件（如 JSON 解析失败），或指向某节点内部的更小子区间。[crates/typst-syntax/src/span.rs:L214-L240](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/span.rs#L214-L240) 的 DiagSpan（16 字节，Span + 额外 u64）与 DiagSpanKind（多一个 sub_range 字段）解决这类需求；[crates/typst-syntax/src/span.rs:L262-L271](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/span.rs#L262-L271) 的 from_span 展示 sub_range 如何打包进 extra。SubRange 定义在 L347 起。DiagSpan 同样改成了手写 Debug（[span.rs:L334-L339](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/span.rs#L334-L339)），打印时直接展示 DiagSpanKind。

#### 4.2.4 代码实践

**实践目标**：亲眼验证「父节点编号小于子节点编号，兄弟从左到右递增」。

1. 阅读现成测试：[crates/typst-syntax/src/node.rs:L1866-L1885](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/node.rs#L1866-L1885) 的 test_linked_node 用 FileId::new_for_test 加 Source::new 构造源码（正是本次从 Source::detached 迁移后的写法），演示了树与 LinkedNode 的配合；span.rs 自己的测试模块在 [crates/typst-syntax/src/span.rs:L604](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/span.rs#L604) 起。
2. 先跑一遍这些内置单元测试：cargo test -p typst-syntax。
3. 再新建 crates/typst-syntax/examples/spans.rs（示例代码，练习后删除）：

```rust
use typst_syntax::{FileId, Source, SyntaxNode};

fn dump(node: &SyntaxNode, depth: usize) {
    // {:?} 现在打印的是 SpanKind 形式，如 Span(Number { id: .., num: .. })
    println!("{}{:?} span={:?}", "  ".repeat(depth), node.kind(), node.span());
    for child in node.children() {
        dump(child, depth + 1);
    }
}

fn main() {
    let source = Source::new(FileId::new_for_test("spans"), "#calc.max(1, 2)");
    dump(source.root(), 0);
}
```

4. 观察现象：同一层级的节点，输出里的 num 从左到右递增；任何内部节点的 num 都小于它所有子孙的 num。得益于本次 Debug 输出的改进，这些编号现在直接可读；若想看原始 64 位值，可把 node.span() 换成 node.span().into_raw()。注意 parse 出来的裸树没有编号（span 都是 detached），必须经过 Source（内部会 numberize）——可以对比 typst_syntax::parse_code("#calc.max(1, 2)") 的裸树输出验证这一点（待本地验证）。
5. 删除示例文件。

#### 4.2.5 小练习与答案

**练习 1**：为什么 Option<Span> 也只占 8 字节？

答案：Span 内部是 NonZeroU64，Rust 会利用「全零不可能出现」这一约束，把 None 编码为全零位模式，无需额外判别位。这是 Rust 标准库的 null 优化（span.rs 文档 L21-L23 明确提到了这一点）。

**练习 2**：两个不同文件里的节点，span 编号可能相同吗？会冲突吗？

答案：可能相同，且不冲突。Span 的高 16 位是 FileId，同一编号搭配不同文件 id 就是不同的 64 位值。反查时 Source::find 先比对 span.id() != Some(self.id())（source.rs L110-L112）就拒绝了跨文件的 span。

**练习 3**：区间形态的 Span（from_range）为什么把 start/end 限制在 23 位（超过会饱和）？

答案：48 位低半区要同时容纳两个数，只能各分 23 位，因此单边上限是 \( 2^{23} - 1 \) 字节。这类 span 用于「把外部文本按 Typst 语法解析」的场景，文档规模可控；超出时饱和裁剪，宁可定位不准也不让类型变大（span.rs L33-L36、L124-L133）。

### 4.3 遍历与反查模式：从 Span 找回源码

#### 4.3.1 概念说明

Span 是「身份证」，但用户最终要看的是源码片段。反查需要两样东西：

- **从 Span 到字节区间**：Source::range（单文件底层 API，直接可用）和 WorldExt::range（编译器全链路的推荐入口，因为它先按 FileId 定位文件再查区间）。
- **LinkedNode**：SyntaxNode 树只存「相对长度」，不存绝对偏移；LinkedNode 在遍历时携带父链与累计偏移，既能给出绝对 range()，也能支撑按编号/区间剪枝查找。

#### 4.3.2 核心流程

```text
调用方拿到 Span
   |  span.get() 展开
   |-- Detached -----------------> 返回 None（无位置）
   |-- Number { id, num } -------> 按 id 找到 Source（World::source 或直接持有）
   |                                 |-- LinkedNode::find_number(num) 剪枝下行
   |                                 v
   |                                 linked.range() = offset..offset+len
   '-- Range { id, range } ------> 直接返回字节区间
```

正向（节点到 Span）则是 O(1) 字段读取 node.span()；批量场景可用 Span::find（span.rs L199-L204）从迭代器里挑第一个非 detached 的 span——典型用法是「一系列候选节点里找一个有真实位置的」。

#### 4.3.3 源码精读

**① LinkedNode：带定位的遍历包装**

[crates/typst-syntax/src/node.rs:L968-L977](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/node.rs#L968-L977) 定义结构：底层节点 + 父链（Rc）+ 在父孩子数组中的下标 + 绝对字节偏移。[crates/typst-syntax/src/node.rs:L979-L1003](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/node.rs#L979-L1003) 提供入口与定位：new(root) 从根开始、range() 返回 offset..offset + node.len()。

[crates/typst-syntax/src/node.rs:L1015-L1022](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/node.rs#L1015-L1022) 的 find 按 SpanKind 分派；[crates/typst-syntax/src/node.rs:L1024-L1055](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/node.rs#L1024-L1055) 的 find_number 文档注释明确写了它依赖的两条有序性保证（父编号小于所有子孙、兄弟从左到右递增），并实现「目标编号小于当前编号即可放弃子树」的剪枝——对照 L1035-L1051 的注释可以看到它连「下一个兄弟编号更大就无需再递归」这层都利用上了。

**② Source 侧的两个入口**

[crates/typst-syntax/src/source.rs:L109-L114](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/source.rs#L109-L114)：Source::find 先校验文件 id 归属，再委托 LinkedNode::new(self.root()).find(span)，返回 LinkedNode（还能顺藤摸瓜拿父节点）：

```rust
pub fn find(&self, span: Span) -> Option<LinkedNode<'_>> {
    if span.id() != Some(self.id()) {
        return None;
    }
    LinkedNode::new(self.root()).find(span)
}
```

[crates/typst-syntax/src/source.rs:L116-L134](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/source.rs#L116-L134)：Source::range 接受 SpanNumber（和可选 SubRange），走 find_number 后返回纯字节区间；文档注释提示「更简单的方式是用 WorldExt::range」。

**③ WorldExt::range：官方推荐入口**

[crates/typst-library/src/lib.rs:L142-L159](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/lib.rs#L142-L159) 为所有 World 实现提供了 blanket 实现：接收 impl Into<DiagSpan>（所以普通 Span 直接可用），按 DiagSpanKind 三分支处理——编号形态先 self.source(id) 找到对应 Source 再调它的 range：

```rust
impl<T: World + ?Sized> WorldExt for T {
    fn range(&self, span: impl Into<DiagSpan>) -> Option<Range<usize>> {
        match span.into().get() {
            DiagSpanKind::Detached => None,
            DiagSpanKind::Number { id, num, sub_range } => {
                self.source(id).ok()?.range(num, sub_range)
            }
            DiagSpanKind::Range { id: _, range } => Some(range),
        }
    }
}
```

#### 4.3.4 代码实践

**实践目标**：完成一次完整反查——从树里取一个 FuncCall，拿到它的 Span，再打印它对应的源码文本。

1. 新建 crates/typst-syntax/examples/lookup.rs（示例代码，练习后删除）：

```rust
use typst_syntax::{FileId, Source, SyntaxNode, ast::{self, AstNode}};

fn first_call(node: &SyntaxNode) -> Option<ast::FuncCall> {
    // 先看自己能不能转成 FuncCall 视图
    if let Some(call) = node.cast::<ast::FuncCall>() {
        return Some(call);
    }
    // 否则在孩子里递归找
    node.children().find_map(first_call)
}

fn main() {
    let source = Source::new(FileId::new_for_test("lookup"), "#calc.max(1, 2)");
    let call = first_call(source.root()).expect("应有函数调用");
    let span = call.span();

    // 反查：拿到带父链的 LinkedNode，取字节区间
    let linked = source.find(span).expect("span 应指向本文件");
    let range = linked.range();
    println!("callee 源码片段: {:?}", &source.text()[range]);
}
```

2. 运行：cargo run -p typst-syntax --example lookup
3. 观察现象：程序打印 calc.max(1, 2)（整个 FuncCall 的源文），而不是只有函数名。
4. 预期结果：call.span() 覆盖整个调用表达式；source.find 到 linked.range 再切片 source.text 这条链把 Span 还原成了文本。如果把输入换成没有函数调用的文本，expect 会如预期般报错。具体输出待本地验证。
5. 删除示例文件。

#### 4.3.5 小练习与答案

**练习 1**：Source::find 和 Source::range 都能定位，选哪个？

答案：要「节点本身及其父链/兄弟上下文」（比如编辑器要高亮外层结构）用 Source::find，返回的 LinkedNode 可以取 parent、range、继续遍历；只要一个 Range<usize>（比如给诊断附加位置）用 Source::range；跨多个文件、或不确定 span 属于哪个文件时，用 WorldExt::range 一步到位。

**练习 2**：为什么 find_number 能剪枝，而「按字节区间找节点」（find_range，分派见 node.rs L1017-L1021，本体在 L1057-L1069）通常不能同等程度剪枝？

答案：剪枝依赖编号的全序不变量（父小于子、兄弟左小右大），这是 numberize 主动维护的数学性质；而字节区间只是节点的派生几何信息，树上有错误节点或零长插入（u2-l3 讲过 Error 节点可为零长）时，区间之间的重叠与包含关系更复杂，无法仅凭比较两端点安全排除子树。

## 5. 综合实践：函数调用收集器

把本讲三个模块串起来：**遍历一段代码的 AST，收集所有函数调用（含嵌套）的函数名与 Span，再通过 Span 打印对应的源码片段。**

1. 新建 crates/typst-syntax/examples/walk_calls.rs（示例代码，练习后删除）：

```rust
use typst_syntax::{FileId, Source, SyntaxNode, ast::{self, AstNode}};

fn callee_name(call: ast::FuncCall) -> String {
    match call.callee() {
        // 直接函数调用，如 text(...)
        ast::Expr::Ident(ident) => ident.as_str().to_string(),
        // 字段访问式调用，如 calc.max(1, 2)
        ast::Expr::FieldAccess(access) => {
            format!("<字段访问>.{}", access.field().as_str())
        }
        // 返回函数的复杂表达式，如 (f())(x)
        _ => "<复杂表达式>".to_string(),
    }
}

fn walk(node: &SyntaxNode, source: &Source) {
    if let Some(call) = node.cast::<ast::FuncCall>() {
        let name = callee_name(call);
        let span = call.span();
        // 模块 3 的反查链路：Span -> LinkedNode -> 字节区间 -> 源码切片
        if let Some(linked) = source.find(span) {
            let range = linked.range();
            println!("调用 {:<14} -> 源码 {:?}", name, &source.text()[range]);
        }
    }
    // 递归：嵌套调用的 callee/args 里还藏着 FuncCall
    for child in node.children() {
        walk(child, source);
    }
}

fn main() {
    let source = Source::new(
        FileId::new_for_test("walk_calls"),
        "#let t = text\n#t(size: 12pt)[hi]\n#calc.max(1, calc.abs(-2))",
    );
    walk(source.root(), &source);
}
```

2. 运行：cargo run -p typst-syntax --example walk_calls
3. 预期输出（示意，待本地验证）：

```text
调用 t              -> 源码 "t(size: 12pt)[hi]"
调用 <字段访问>.max -> 源码 "calc.max(1, calc.abs(-2))"
调用 <字段访问>.abs -> 源码 "calc.abs(-2)"
```

4. 观察要点：
   - 递归 children() 遍历能钻进 callee 和 Args 内部，所以嵌套的 calc.abs 也被抓到；
   - callee_name 里 FieldAccess::field() 返回的 Ident 直接 as_str() 取文本——模块 1 的视图方法在实战中的用法；
   - 每个片段都靠模块 3 的 span、find、range、text 切片链路还原；
   - 想进一步区分参数形态，可对 call.args().items() 逐个匹配 Arg 的 Pos/Named/Spread 变体（L2177-L2184）。
5. 进阶实验：把文档改成有语法错误的形式（如 #calc.max(1,），观察收集器输出什么、为什么不 panic（提示：容错树与 placeholder 机制）。
6. 完成后删除示例文件，保持工作区干净。

## 6. 本讲小结

- **一棵树、两个视图**：AST 不是第二棵树，而是 repr(transparent) 的 &SyntaxNode 包装；cast 按 SyntaxKind 验明正身，node! 宏批量生成这些零成本包装。
- **语义访问器靠「在孩子里捞」实现**：cast_first/cast_last 跳过 trivia 与无关 token，找不到时用 placeholder() 兜底，永不 panic——容错解析哲学在 AST 层的延续。
- **Expr 枚举是三种模式的汇合点**：markup/math/code 的表达式统一从 Markup::exprs 迭代出来，typst-eval 的大 match 消费它。
- **Span 是 8 字节编号**：高 16 位文件 id 加低 48 位编号；编号在 Source::new 时分配，父小于子、兄弟从左到右递增，支持剪枝查找。本次更新后 Span/DiagSpan 的 Debug 输出直接展示解包后的 SpanKind/DiagSpanKind，打印即可读。
- **编号式设计服务增量编译**：编辑时编号基本稳定，Span 可作为 comemo 记忆化的输入而不炸缓存。
- **反查链路**：node.span() 正向 O(1)；反向用 Source::find/Source::range（单文件）或 WorldExt::range（跨文件，官方推荐），核心数据结构是携带绝对偏移的 LinkedNode。
- **API 迁移备忘**：`Source::detached` 已移除——测试与示例现在用 `Source::new(FileId::new_for_test("名字"), "文本")`，且 `Source::new` 接受任何 `impl Into<String>`。

## 7. 下一步学习建议

- **下一讲 u2-l5（增量解析与语法高亮）**：本讲的 numberize「只占区间左半、留出空隙」策略正是为增量重解析准备的——编辑后只对受影响区间重新编号；重构后的 InnerNode::numberize 新增 `Option<&mut Span>` 与孩子区间参数，正是增量重编号的接口基础。highlight.rs 则会把本讲的 SyntaxKind 映射成编辑器高亮 Tag。
- **巩固练习**：通读 [crates/typst-syntax/src/ast.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-syntax/src/ast.rs) 的模块文档（L1-L54）与 node! 调用清单，试着自己给一个冷门节点（如 MathFrac）画出「CST 孩子、AST 语义方法」的对应关系。
- **向后展望**：u3-l6 会看到 typst-eval 如何用本讲的视图驱动求值（impl Eval for ast::FuncCall 在 call.rs L24）；u5-l6 的 typst-ide（自动补全、定义跳转）则大量复用 Span、LinkedNode、Source::find 这套定位机制。
