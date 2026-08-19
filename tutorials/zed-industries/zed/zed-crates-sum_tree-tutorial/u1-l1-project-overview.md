# sum_tree 是什么:Zed 的并发友好 B+ 树

## 1. 本讲目标

学完本讲,你应该能够:

1. 用自己的话说清 sum_tree 解决的问题:**O(log n) 的前缀汇总查询** + **可结构共享的持久化有序序列**。
2. 知道 sum_tree 在 Zed 中的地位:它是 rope、multi_buffer、buffer_diff、language、editor 等 13 个 crate 的共同底层依赖。
3. 说出 crate 四个源码文件(`sum_tree.rs` / `cursor.rs` / `tree_map.rs` / `property_test.rs`)各自的职责。
4. 读懂 `Cargo.toml` 中 `[lib]` 与 `[features]` 的配置,理解 heapless、rayon、proptest 等依赖分别在 crate 中扮演什么角色。
5. 在本地完成 `cargo build -p sum_tree` 与 `cargo test -p sum_tree`,并仿照现有测试写出自己的第一个 SumTree 测试。

## 2. 前置知识

本讲假设你了解以下概念,不熟悉的话先看这里的通俗解释:

- **有序序列**:像 `Vec<u8>` 一样,元素排成一列,有头有尾。文本编辑器里的字符序列就是最典型的有序序列。
- **前缀和(prefix sum)**:如果已知每一段的"总量"(比如每行有多少个字符),那么"前 k 段加起来总共多少"就可以一段一段累加,而不必逐个元素数一遍。sum_tree 的核心思想就是把这种"每段的总量"缓存在树的节点里。
- **B+ 树的直觉**:一种多叉树。所有真正的数据都放在**叶子节点**;内部节点只存"路由信息"(子节点的汇总),用来快速跳到目标叶子。每个节点容纳的子节点数有上限和下限,所以树保持矮胖,从根到叶只需经过很少几层。查询代价近似与树高成正比,即 \( O(\log n) \)。
- **结构共享与持久化数据结构**:用 `Arc`(原子引用计数的智能指针)包裹节点后,克隆一棵树只是克隆根指针;两棵"版本不同"的树可以共享没被修改过的整棵子树。这就是编辑器能廉价保留历史快照、多线程各持一份视图的原因。
- **Rust 基础**:trait、泛型关联类型、`Arc`、模块系统(`mod` / `pub use`)、Cargo 的 manifest(`Cargo.toml`)。
- **永久链接的读法**:本讲所有源码引用都指向 GitHub 上某次提交的固定行号,格式如 `文件路径#L起始-L结束`,你可以直接点击跳转,和本地文件对照阅读。

## 3. 本讲源码地图

| 文件 | 行数 | 作用 |
| --- | --- | --- |
| [crates/sum_tree/Cargo.toml](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/Cargo.toml#L1-L35) | 35 | crate 清单:包元信息、库根路径、依赖与 feature 定义 |
| [crates/sum_tree/src/sum_tree.rs](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1-L1903) | 1903 | **crate 根模块**。定义 `Item`/`Summary`/`Dimension` 等 trait、`SumTree`/`Node` 数据结构、构建/查询/编辑 API(`from_iter`、`extend`、`append`、`items`、`extent` 等),文件末尾是完整的 `#[cfg(test)] mod tests` |
| [crates/sum_tree/src/cursor.rs](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L1-L861) | 861 | 游标 `Cursor` 与过滤游标 `FilterCursor`、迭代器 `Iter`:在树上按维度定位、前后移动、切片、聚合 |
| [crates/sum_tree/src/tree_map.rs](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/tree_map.rs#L1-L531) | 531 | 基于 `SumTree` 实现的有序映射 `TreeMap` 与有序集合 `TreeSet`(把"最大键"当作 summary) |
| [crates/sum_tree/src/property_test.rs](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/property_test.rs#L1-L32) | 32 | 为 proptest 提供 `SumTree` 的 `Arbitrary` 实现与 `sum_tree(...)` 生成策略,只在测试或 `test-support` feature 下编译 |

整个 crate 一共只有 4 个源码文件、3300 多行,却支撑了 Zed 最核心的文本数据结构。这是本手册选择"精读"而不是"泛读"它的原因。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块:

1. **sum_tree 解决什么问题**——从编辑器的需求推出"带汇总的 B+ 树"这个设计。
2. **crate 根模块**——`mod` 与 `pub use` 如何组织出极小的对外 API 面。
3. **Cargo.toml**——`[lib]`、`[features]` 与各依赖的用途。

### 4.1 sum_tree 解决什么问题:带汇总的并发友好 B+ 树

#### 4.1.1 概念说明

Cargo.toml 里对这个 crate 的一句官方定位是:

> A sum tree data structure, a concurrency-friendly B-tree
> (一种 sum tree 数据结构,一棵对并发友好的 B 树)

拆开理解,Zed 需要一种有序序列结构,同时满足三个苛刻条件:

1. **快速的前缀汇总查询**。编辑器每帧都在问:"第 1000 行第 5 列对应字节偏移多少?""这个选区覆盖了多少字符?"这类问题本质是前缀和查询。用 `Vec` 存文本,任何一次编辑都会让缓存的前缀和全部失效,重算是 \( O(n) \)。
2. **廉价的快照与多版本共存**。Zed 支持协作编辑、撤销重做、后台 diff。每个协作者、每次撤销都相当于持有一个"历史版本"。如果每次编辑都复制整个 buffer,内存会被拖垮。
3. **有序插入/删除仍保持 \( O(\log n) \)**。不能像链表那样插入便宜查询昂贵。

sum_tree 的答案就是"给 B+ 树的每个节点额外缓存一份 Summary(汇总)":

- 叶子节点缓存**每个元素**的 summary 和整叶的 summary;
- 内部节点缓存**每个子树**的 summary 和整个子树的 summary;
- 查询时从根往下走,每一层只需把目标与 `child_summaries` 比较,就能**整棵跳过**不相关的子树——查询代价是树高 \( O(\log n) \),而不是元素数 \( O(n) \)。

而"并发友好"体现在:整棵树由 `Arc<Node>` 串起来,是不可变的(函数式)结构。所谓"修改"是沿着一条根到叶的路径复制出新节点,其余子树原样共享。于是克隆一个"旧版本"的成本接近于一次指针递增,这就是 `TreeMap` 文档里 "cheaply-cloneable(可廉价克隆)" 的含义。

#### 4.1.2 核心流程

一次典型的"构建 + 查询"流程:

```text
构建(自底向上):
  迭代器 → 每 2*TREE_BASE 个元素装成一个 Leaf(逐个求 summary)
        → 若 Leaf 数量 > 1,每 2*TREE_BASE 棵子树组装成一个 Internal(高度 +1)
        → 重复上一层,直到只剩一个根

查询(自顶向下):
  从根出发,用目标维度值与每个 child_summary 累加比较
  → 跳过整棵"还没到目标"的子树(O(1) 判断,O(子树元素数) 的节省)
  → 到达叶子,得到目标元素
```

用数学语言说:设元素总数为 \( n \),节点容量上限为 \( 2 \cdot \text{TREE\_BASE} \)(常数),则树高 \( h \approx \log_{2 \cdot \text{TREE\_BASE}} n \),一次定位只需访问 \( h + 1 \) 个节点。

#### 4.1.3 源码精读

**① `SumTree` 结构:一个 `Arc<Node>` 的包装**

[crates/sum_tree/src/sum_tree.rs:L206-L213](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L206-L213)

```rust
/// A B+ tree in which each leaf node contains `Item`s of type `T` and a `Summary`s for each `Item`.
/// Each internal node contains a `Summary` of the items in its subtree.
///
/// The maximum number of items per node is `TREE_BASE * 2`.
#[derive(Clone)]
pub struct SumTree<T: Item>(Arc<Node<T>>);
```

这段是整棵树的"总纲":叶子存元素和逐元素 summary;内部节点只存子树 summary;单节点容量上限是 `TREE_BASE * 2`;`#[derive(Clone)]` 加上 `Arc` 意味着克隆整棵树只是克隆一个引用计数的根。

**② `Node` 枚举:Internal 与 Leaf 两个变体**

[crates/sum_tree/src/sum_tree.rs:L1268-L1281](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1268-L1281)

```rust
pub enum Node<T: Item> {
    Internal {
        height: u8,
        summary: T::Summary,
        child_summaries: ArrayVec<T::Summary, { 2 * TREE_BASE }, u8>,
        child_trees: ArrayVec<SumTree<T>, { 2 * TREE_BASE }, u8>,
    },
    Leaf {
        summary: T::Summary,
        items: ArrayVec<T, { 2 * TREE_BASE }, u8>,
        item_summaries: ArrayVec<T::Summary, { 2 * TREE_BASE }, u8>,
    },
}
```

- `Internal` 记录 `height`(叶子高度记为 0)、自身的 `summary`、每个子树的 summary 数组和子树数组——查询时靠 `child_summaries` 决定往哪棵子树走。
- `Leaf` 记录元素数组 `items` 和逐元素的 `item_summaries`。
- `ArrayVec` 是 `heapless::Vec` 的别名(见 [sum_tree.rs:L7](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L7)),容量 `{ 2 * TREE_BASE }` 在**编译期**固定,节点内容直接内联在节点里,不再为每个数组单独做堆分配。

**③ `TREE_BASE`:测试与正式环境的容量切换**

[crates/sum_tree/src/sum_tree.rs:L15-L18](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L15-L18)

```rust
#[cfg(test)]
pub const TREE_BASE: usize = 2;
#[cfg(not(test))]
pub const TREE_BASE: usize = 6;
```

测试构建下单节点最多 4 个元素——这样几十个元素就能触发分裂、多层等所有边界情况;正式构建下单节点最多 12 个元素,树更矮。这是手写数据结构里非常实用的"小容量测试法"。

**④ `from_iter`:自底向上逐层组装**

[crates/sum_tree/src/sum_tree.rs:L249-L316](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L249-L316)

```rust
pub fn from_iter<I: IntoIterator<Item = T>>(...) -> Self {
    let mut nodes = Vec::new();
    let mut iter = iter.into_iter().fuse().peekable();
    while iter.peek().is_some() {
        let items: ArrayVec<T, { 2 * TREE_BASE }, u8> =
            iter.by_ref().take(2 * TREE_BASE).collect();
        // ... 求 item_summaries、整叶 summary,包装成 Leaf
    }
    while nodes.len() > 1 {
        height += 1;
        // ... 每 2*TREE_BASE 棵子树合并成一个 Internal
    }
}
```

关键行为(后面实践会用到):**每次取满 `2 * TREE_BASE` 个元素装成一个叶子,最后一页允许不满**;然后每 `2 * TREE_BASE` 棵子树向上组装一层,直到只剩一个根。

**⑤ `extend` 与 `append`:所有构建入口的汇聚点**

[crates/sum_tree/src/sum_tree.rs:L750-L755](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L750-L755)

```rust
pub fn extend<I>(&mut self, iter: I, cx: ...) -> Self
where
    I: IntoIterator<Item = T>,
{
    self.append(Self::from_iter(iter, cx), cx);
}
```

`extend` = 先用 `from_iter` 把迭代器变成一棵树,再 `append` 拼接到自身。`push`、`par_extend` 同样最终落到 `append`(见 [L768-L794](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L768-L794))。append 的内部分裂逻辑是第 4 单元(u4)的主角,本讲只认识它的"门面"。

**⑥ `extent` 与 `items`:读出整树汇总与全部元素**

[crates/sum_tree/src/sum_tree.rs:L723-L734](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L723-L734)

```rust
pub fn extent<'a, D: Dimension<'a, T::Summary>>(&'a self, cx: ...) -> D {
    let mut extent = D::zero(cx);
    match self.0.as_ref() {
        Node::Internal { summary, .. } | Node::Leaf { summary, .. } => {
            extent.add_summary(summary, cx);
        }
    }
    extent
}
```

`extent` 只读根节点缓存好的 `summary`,是 \( O(1) \) 的——这就是"每个节点带汇总"直接兑现的红利:"整棵树有几个元素?"不需要遍历。

[crates/sum_tree/src/sum_tree.rs:L381-L390](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L381-L390)

```rust
pub fn items<'a>(&'a self, cx: ...) -> Vec<T> {
    let mut items = Vec::new();
    let mut cursor = self.cursor::<()>(cx);
    cursor.next();
    while let Some(item) = cursor.item() {
        items.push(item.clone());
        cursor.next();
    }
    items
}
```

`items` 用游标从头走到尾收集所有元素,主要供测试断言使用(源码里也标了 `#[allow(unused)]`,因为正式构建中几乎没有调用方)。它是本讲实践中最重要的"验收工具"。

#### 4.1.4 代码实践

**实践一(本讲主实践):跑通构建与测试,写出你的第一个 SumTree 测试**

1. **实践目标**:确认本地环境能构建、能测试 sum_tree;并亲手用 `SumTree::default() + extend(0..10, ())` 构建一棵树,断言其内容。

2. **操作步骤**:

   1. 在 Zed 仓库根目录(必须是 workspace 根,sum_tree 是 workspace 成员,不能进入子目录单独构建)执行:

      ```bash
      cargo build -p sum_tree
      cargo test -p sum_tree
      ```

   2. 打开 `crates/sum_tree/src/sum_tree.rs`,定位到文件末尾的测试模块中的 `test_extend_and_push_tree`([L1404-L1414](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1404-L1414)):

      ```rust
      #[test]
      fn test_extend_and_push_tree() {
          let mut tree1 = SumTree::default();
          tree1.extend(0..20, ());

          let mut tree2 = SumTree::default();
          tree2.extend(50..100, ());

          tree1.append(tree2, ());
          assert_eq!(tree1.items(()), (0..20).chain(50..100).collect::<Vec<u8>>());
      }
      ```

   3. 在它旁边**仿照**写一个属于你的测试(这是示例代码,验证后请还原,不要提交到仓库):

      ```rust
      // 示例代码:学习用,验证后请删除
      #[test]
      fn my_first_tree() {
          let mut tree = SumTree::default();
          tree.extend(0..10, ());
          assert_eq!(tree.items(()), (0..10).collect::<Vec<u8>>());
          println!("{:#?}", tree);
      }
      ```

   4. 只跑这一个测试,并显示打印输出:

      ```bash
      cargo test -p sum_tree my_first_tree -- --nocapture
      ```

3. **需要观察的现象**:

   - `cargo test -p sum_tree` 全部通过;
   - `-- --nocapture` 会把整棵树以 `Debug` 格式pretty打印出来(因为 `SumTree` 实现了 `Debug`,见 [L215-L223](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L215-L223));
   - 按 `from_iter` 的分组规则推演:测试构建下 `TREE_BASE = 2`,每叶最多 `2 * 2 = 4` 个元素,10 个元素应分成 `[0,1,2,3]`、`[4,5,6,7]`、`[8,9]` 三片叶子;三棵子树多于 1,于是向上组装出一个 `height: 1` 的 Internal 根。

4. **预期结果**:测试通过;Debug 输出的结构(叶子分组、根的 height、每层 summary)与上面的推演一致。具体打印格式以本地运行为准,**待本地验证**。

5. 一个值得现在就想清楚的细节:为什么 `SumTree::default()` 对 `SumTree<u8>` 可用?答案在 [L1258-L1266](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1258-L1266)——`Default` 只对 `Summary::Context<'a> = ()`(即"无上下文"的 summary)实现;而测试模块里为 `u8` 实现的 `IntegersSummary` 是一个 `ContextLessSummary`(见 [L1855-L1866](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1855-L1866)),自动获得 `Context<'a> = ()`,所以那些到处出现的 `()` 参数就是"上下文"占位。

#### 4.1.5 小练习与答案

**练习 1**:同样是"查询前 1000 个元素的总字符数",用 `Vec<String>` 前缀和缓存与用 sum_tree 有什么本质区别?

**参考答案**:`Vec` 的前缀和是一个"外部缓存",任何一次插入/删除都会使其后的所有前缀和失效,重算代价 \( O(n) \);sum_tree 把 summary 存在节点内部,编辑只影响根到叶一条路径上的节点(每层至多常数个),其余节点的 summary 依然有效,且任何子树的汇总都能 \( O(1) \) 直接读到。

**练习 2**:`extent::<Count>()` 为什么是 \( O(1) \) 而不需要遍历整棵树?

**参考答案**:见 [sum_tree.rs:L723-L734](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L723-L734),根节点(`Internal` 或 `Leaf`)的 `summary` 字段始终缓存着整棵树的汇总,`extent` 只是把它投影成目标维度,只匹配一次、不递归。

**练习 3**:如果让你为"单节点最多容纳 12 个元素"的树设计测试,你会怎么让几十个元素就触发多层结构?

**参考答案**:sum_tree 的做法是编译期切换 `TREE_BASE`(测试下取 2,见 [L15-L18](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L15-L18)),这样小规模数据就能覆盖分裂、欠溢、多层遍历等边界。这正是本讲实践中 10 个元素就能看到 `height: 1` 根节点的原因。

### 4.2 crate 根模块:`mod` 声明与极小的 `pub use` API 面

#### 4.2.1 概念说明

Rust 中一个 crate 的"库根"文件默认是 `src/lib.rs`,但 Zed 的规范是给库根起有意义的名字(sum_tree 的规范见仓库 CLAUDE.md:优先用 `[lib] path = "..."rs` 指定库根)。sum_tree 的库根就是 `src/sum_tree.rs`。

根模块承担两件事:

1. 用 `mod` 声明子模块,把实现拆到 `cursor.rs`、`tree_map.rs` 等文件;
2. 用 `pub use` 把真正想暴露的类型**重新导出**,同时让子模块本身保持私有——外部只能"按需进口",crate 的公共 API 面因此非常小、非常稳定。

#### 4.2.2 核心流程

```text
外部使用者                     crate 内部
use sum_tree::SumTree;   ──▶  sum_tree.rs(根,直接定义 SumTree)
use sum_tree::Cursor;    ──▶  pub use cursor::{Cursor, ...};  ──▶ 私有 mod cursor
use sum_tree::TreeMap;   ──▶  pub use tree_map::{...};        ──▶ 私有 mod tree_map
(仅测试/test-support)     ──▶  pub mod property_test;         ──▶ cfg 门控的模块
```

#### 4.2.3 源码精读

**① 根模块的全部模块声明与重导出**

[crates/sum_tree/src/sum_tree.rs:L1-L13](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1-L13)

```rust
mod cursor;
#[cfg(any(test, feature = "test-support"))]
pub mod property_test;
mod tree_map;

pub use cursor::{Cursor, FilterCursor, Iter};
use heapless::Vec as ArrayVec;
...
pub use tree_map::{MapSeekTarget, TreeMap, TreeSet};
```

逐行解读:

- `mod cursor;` / `mod tree_map;`——私有子模块,文件外**无法**通过 `sum_tree::cursor::Cursor` 访问,只能用重导出的 `sum_tree::Cursor`。
- `#[cfg(any(test, feature = "test-support"))] pub mod property_test;`——属性测试工具只在两种情况下编译:本 crate 自己跑测试(`test`),或下游 crate 开启了 `test-support` feature。正式产物里完全不包含它。
- `pub use cursor::{Cursor, FilterCursor, Iter};`——把游标三件套挂到 crate 顶层。
- `use heapless::Vec as ArrayVec;`——把 heapless 的定长 `Vec` 别名为 `ArrayVec`,强调"容量编译期固定"这一心智模型。

**② property_test 模块:给下游的"测试工具箱"**

[crates/sum_tree/src/property_test.rs:L7-L20](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/property_test.rs#L7-L20)

```rust
impl<T> Arbitrary for SumTree<T>
where
    T: Debug + Arbitrary + Item + 'static,
    T::Summary: Debug + Summary<Context<'static> = ()>,
{
    ...
    fn arbitrary_with((): Self::Parameters) -> Self::Strategy {
        any::<Vec<T>>()
            .prop_map(|vec| SumTree::from_iter(vec, ()))
            .boxed()
    }
}
```

它让任何"元素满足 `Arbitrary`"的 `SumTree` 都能直接用于 proptest 属性测试(生成策略就是:随机一个 `Vec`,再 `from_iter` 成树)。配合 [property_test.rs:L25-L32](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/property_test.rs#L25-L32) 的 `sum_tree(values, size)` 策略,下游 crate(如 rope 的测试)可以方便地生成指定规模的随机树。详细用法在第 5 单元(u5-l3)展开。

**③ cursor.rs 与 tree_map.rs 的入口一瞥**

[crates/sum_tree/src/cursor.rs:L29-L37](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/cursor.rs#L29-L37)

```rust
pub struct Cursor<'a, 'b, T: Item, D> {
    tree: &'a SumTree<T>,
    stack: ArrayVec<StackEntry<'a, T, D>, 16, u8>,
    pub position: D,
    did_seek: bool,
    at_end: bool,
    ...
}
```

`Cursor` 用一个容量 16 的栈模拟"根到叶子"的路径,是所有读操作的中枢(第 3 单元主角)。

[crates/sum_tree/src/tree_map.rs:L5-L10](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/tree_map.rs#L5-L10)

```rust
/// A cheaply-cloneable ordered map based on a [SumTree](crate::SumTree).
#[derive(Clone, PartialEq, Eq)]
pub struct TreeMap<K, V>(SumTree<MapEntry<K, V>>)
```

`TreeMap` 是 `SumTree` 的直接应用:把键值对当元素,把"最大键"当 summary,就得到一个可廉价克隆的有序映射(第 5 单元主角)。

#### 4.2.4 代码实践

**实践二(源码阅读型):数一数 crate 的公共 API 面**

1. **实践目标**:直观感受"`mod` 私有 + `pub use` 重导出"把 API 面收敛到多小。

2. **操作步骤**:

   1. 生成并浏览 crate 文档(只包含公开项):

      ```bash
      cargo doc -p sum_tree --no-deps --open
      ```

   2. 在左侧目录里核对,公开类型应当只有根模块定义与重导出的这些:`SumTree`、`Cursor`、`FilterCursor`、`Iter`、`TreeMap`、`TreeSet`、`MapSeekTarget`,以及一组 trait(`Item`、`KeyedItem`、`Summary`、`ContextLessSummary`、`Dimension`、`SeekTarget`、`Edit`、`Bias`、`Dimensions`、`NoSummary` 等)。
   3. 尝试在文档里搜索 `StackEntry`、`MapEntry`——它们存在(`cursor.rs` L7、`tree_map.rs` L13)但因为模块私有而不会出现在文档中。

3. **需要观察的现象**:文档页面结构非常"扁",没有 `cursor::`、`tree_map::` 这样的模块层级。

4. **预期结果**:与第 2 步的清单一致;`StackEntry` 搜不到。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**:为什么 `cursor` 模块用 `mod cursor;` 而不是 `pub mod cursor;`,却还要 `pub use cursor::{Cursor, ...};`?

**参考答案**:私有 `mod` 让实现细节(如 `StackEntry`、`seek_internal` 等)天然对外不可见;`pub use` 只挑出稳定的公共类型挂到顶层。这样内部可以自由重构,只要重导出的名字不变,下游就不会破坏。

**练习 2**:`#[cfg(any(test, feature = "test-support"))]` 这行门控解决了什么问题?如果不加会有什么后果?

**参考答案**:它让 `property_test` 模块(及其引入的 proptest 依赖)只在"本 crate 测试"或"下游显式开启 test-support feature"时编译。不加的话,要么正式产物被 proptest 拖累(如果写成无条件 `pub mod` 且 proptest 不是 optional),要么下游 crate 想复用这些测试工具时无路可走。

**练习 3**:某个下游 crate 想在自己的测试里生成随机 `SumTree`,它需要做哪两件事?

**参考答案**:一是在其 `Cargo.toml` 中给 sum_tree 加上 `features = ["test-support"]`(该 feature 会拉起 optional 的 proptest 依赖);二是 `use sum_tree::property_test::sum_tree;` 之后用 `sum_tree(any::<u8>(), 0..100)` 这类策略生成随机树(见 [property_test.rs:L25-L32](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/property_test.rs#L25-L32))。

### 4.3 Cargo.toml:`[lib]`、features 与依赖构成

#### 4.3.1 概念说明

`Cargo.toml` 是 crate 的"身份证 + 采购清单"。sum_tree 的这份清单信息密度很高:它同时回答了"这个 crate 是什么"(description)、"库根在哪"(`[lib]`)、"依赖什么、为什么依赖"(`[dependencies]` / `[dev-dependencies]`)、"给下游留了什么开关"(`[features]`)。

#### 4.3.2 核心流程

```text
Cargo.toml
  ├─ [package]        description = "concurrency-friendly B-tree"(官方定位)
  ├─ [lib]            path = "src/sum_tree.rs"   ← 库根不用 lib.rs
  │                   doctest = false            ← 不编译文档示例测试
  ├─ [dependencies]   heapless / rayon / log / ztracing / tracing
  │                   └─ proptest(optional,由 feature 控制)
  ├─ [dev-dependencies] ctor / rand / proptest / zlog(仅测试时)
  └─ [features]       test-support = ["proptest"]
```

依赖各自的角色:

| 依赖 | 类别 | 在 crate 中的用途 |
| --- | --- | --- |
| heapless(0.9) | 正式依赖 | 提供定容 `Vec`(即 `ArrayVec`),让每个节点的 items/summaries/children 内联存储、容量编译期固定,避免节点内部再堆分配 |
| rayon(1.8) | 正式依赖 | 数据并行框架,支撑 `from_par_iter` / `par_extend` 并行建树 |
| log / ztracing / tracing | 正式依赖 | 日志与性能埋点(如 `find_exact` 上的 `#[instrument]`) |
| proptest | optional 正式依赖 | 属性测试框架,仅 `test-support` feature 拉起,供 `property_test` 模块使用 |
| ctor、rand、zlog | dev-dependencies | 仅测试:ctor 自动初始化测试日志、rand 驱动随机化对拍测试、zlog 提供测试日志后端 |

版本来自仓库根的 workspace 清单([Cargo.toml:L592-L765](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/Cargo.toml#L592-L765),如 `heapless = "0.9.2"`、`rayon = "1.8"`、`rand = "0.9"`、`ctor = "1.0.12"`),由 `workspace = true` 统一继承,Zed 全仓库版本一致。

#### 4.3.3 源码精读

**① 完整的 Cargo.toml(只有 35 行,值得整读)**

[crates/sum_tree/Cargo.toml:L1-L35](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/Cargo.toml#L1-L35)

```toml
[package]
name = "sum_tree"
version = "0.1.0"
edition.workspace = true
publish = false
license = "Apache-2.0"
description = "A sum tree data structure, a concurrency-friendly B-tree"

[lib]
path = "src/sum_tree.rs"
doctest = false

[dependencies]
heapless.workspace = true
rayon.workspace = true
log.workspace = true
ztracing.workspace = true
tracing.workspace = true
proptest = { workspace = true, optional = true }

[dev-dependencies]
ctor.workspace = true
rand.workspace = true
proptest.workspace = true
zlog.workspace = true

[features]
test-support = ["proptest"]
```

分块解读:

- **[L1-L7](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/Cargo.toml#L1-L7)** 包元信息。`publish = false` 表示不发布到 crates.io(Zed 的 crate 只在仓库内使用);`description` 就是本讲反复引用的官方一句话定位。
- **[L12-L14](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/Cargo.toml#L12-L14)** `[lib] path = "src/sum_tree.rs"` 显式指定库根文件名与 crate 同名(遵循 Zed "不用 lib.rs" 的规范);`doctest = false` 关闭文档测试编译。
- **[L22](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/Cargo.toml#L22)** `proptest = { workspace = true, optional = true }`——optional 依赖默认定义一个同名 feature,于是有了下一行。
- **[L34-L35](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/Cargo.toml#L34-L35)** `test-support = ["proptest"]`——对外的测试工具开关:下游声明 `features = ["test-support"]` 即可使用 `property_test` 模块(与根模块 [sum_tree.rs:L2-L3](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L2-L3) 的 cfg 门控正好呼应)。

**② 谁在依赖 sum_tree:它在 Zed 中的位置**

在仓库中检索 `Cargo.toml`,共有 13 个 crate 声明依赖 sum_tree:rope、text、multi_buffer、buffer_diff、language、editor、gpui、git、project、markdown、notifications、copilot、worktree。

也就是说,你在 Zed 里敲下的每一个字符,最终都落在 `SumTree<Chunk>`(rope 的文本块树)这类结构里。注意以上是**直接依赖**关系(逐个检索各 crate 的 Cargo.toml 得到);从 editor 到 sum_tree 的完整调用链要经过多层传递,本讲不展开,详见第 5 单元 u5-l2。

#### 4.3.4 代码实践

**实践三:观察依赖树与 feature 的效果**

1. **实践目标**:亲眼看到 `test-support` feature 如何改变编译内容。

2. **操作步骤**:

   1. 查看默认依赖图:

      ```bash
      cargo tree -p sum_tree
      ```

   2. 开启 feature 再看一次:

      ```bash
      cargo tree -p sum_tree --features test-support
      ```

   3. (选做)验证带 feature 的构建可通过:

      ```bash
      cargo build -p sum_tree --features test-support
      ```

3. **需要观察的现象**:第 1 次输出的依赖里**没有** proptest;第 2 次输出里 proptest(及其依赖)出现了。

4. **预期结果**:与 `[features] test-support = ["proptest"]` 的语义一致。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**:为什么 proptest 同时出现在 `[dependencies]`(带 optional)和 `[dev-dependencies]`?

**参考答案**:`[dev-dependencies]` 里的 proptest 供**本 crate 自己的测试**使用(始终拉起);`[dependencies]` 里的 optional proptest 是为了让**下游 crate** 开启 `test-support` 时,`property_test` 模块(它属于正式代码路径、受 cfg 门控)也能引用 proptest——dev-dependencies 是不会传播给下游的。

**练习 2**:`[lib] path = "src/sum_tree.rs"` 不写会怎样?

**参考答案**:Cargo 默认找 `src/lib.rs`,而这个 crate 没有(也没有 `mod.rs`,均为 Zed 规范所避免)。不写这行,crate 将没有库目标,`cargo build -p sum_tree` 直接失败。

**练习 3**:heapless 在这个 crate 里解决了什么问题?换成普通 `std::vec::Vec` 行不行?

**参考答案**:节点内的 items / item_summaries / child_summaries / child_trees 用定容 `ArrayVec`(`heapless::Vec`)存储,内容内联在节点分配里,克隆/修改节点时不会有额外的小额堆分配,对这种"节点极小、数量极大"的结构很关键。换成 `Vec` 功能上可行,但每个节点会多出一次堆分配和一层间接,`Arc::make_mut` 写时克隆的代价也随之上升。

## 5. 综合实践

把本讲三个模块串成一个任务——**"从零跑通,并留下你的第一个测试"**:

1. **构建与测试**:在仓库根执行 `cargo build -p sum_tree` 与 `cargo test -p sum_tree`,确认全绿(模块 4.3 的成果:你知道了 `-p sum_tree` 之所以能用,是因为它是 workspace 成员)。
2. **写测试**:在 `crates/sum_tree/src/sum_tree.rs` 的 `mod tests` 中,紧挨着 `test_extend_and_push_tree` 仿写:

   ```rust
   // 示例代码:学习用,验证后请删除
   #[test]
   fn my_first_sum_tree() {
       let mut tree = SumTree::default();
       tree.extend(0..10, ());
       assert_eq!(tree.items(()), (0..10).collect::<Vec<u8>>());
       println!("{:#?}", tree);
   }
   ```

3. **单测运行**:`cargo test -p sum_tree my_first_sum_tree -- --nocapture`,观察 Debug 打印出的树。
4. **对照源码解释你看到的三件事**(把答案写进你的笔记):
   - 为什么 `SumTree::default()` 可用、`extend` 的第二个参数是 `()`?(提示:[sum_tree.rs:L1258-L1266](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1258-L1266) 与 `ContextLessSummary`)
   - 10 个元素为什么分成 `[0,1,2,3]`、`[4,5,6,7]`、`[8,9]` 三片叶子?(提示:[sum_tree.rs:L249-L316](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L249-L316) 的 `take(2 * TREE_BASE)`,以及测试构建下 `TREE_BASE = 2`)
   - 根节点是 `Internal` 且 `height: 1`,它的 `summary` 里 `count` 是多少?和 `tree.extent::<Count>(())` 有什么关系?(提示:[sum_tree.rs:L723-L734](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L723-L734);`Count` 定义在 [L1828-L1829](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/sum_tree/src/sum_tree.rs#L1828-L1829))
5. **收尾**:删除你添加的测试,恢复文件原状(`git checkout -- crates/sum_tree/src/sum_tree.rs` 或 `git restore`),保持仓库干净。

完成后,你就拥有了后续所有讲义共用的"实验台":能构建、能测试、能往 `mod tests` 里加自己的验证代码。

## 6. 本讲小结

- sum_tree 是 Zed 的**并发友好 B+ 树**:叶子存元素、内部节点存子树汇总,单节点容量上限 `2 * TREE_BASE`(测试构建 4 / 正式构建 12)。
- 它同时解决"**O(log n) 前缀汇总查询**"与"**廉价快照**"两个问题:查询靠节点级 summary 整棵跳过子树,快照靠 `Arc<Node>` 的结构共享,克隆只是克隆根指针。
- crate 只有 4 个源码文件:`sum_tree.rs`(根:trait + `SumTree`/`Node` + 全部构建/编辑 API)、`cursor.rs`(游标读取)、`tree_map.rs`(`TreeMap`/`TreeSet` 应用层)、`property_test.rs`(proptest 工具,受 cfg 门控)。
- 根模块用"私有 `mod` + 精选 `pub use`"把公共 API 面收敛到 `SumTree`、`Cursor`、`TreeMap` 等少数名字;`property_test` 仅在 `test` 或 `test-support` feature 下编译。
- `Cargo.toml` 三看点:`[lib] path = "src/sum_tree.rs"`(库根与 crate 同名)、`test-support = ["proptest"]`(给下游的测试开关)、heapless/rayon 分别负责定容节点存储与并行建树。
- 它被 rope、text、multi_buffer、buffer_diff、language、editor 等 13 个 crate 依赖,是 Zed 文本栈的地基。

## 7. 下一步学习建议

- **下一讲(u1-l2)《树的骨架:Node、TREE_BASE 与 Arc 结构共享》**:深入 `Node` 的 `Internal`/`Leaf` 布局、`ArrayVec` 定容存储、测试与正式构建的 `TREE_BASE` 切换,以及 `Arc::make_mut` 的写时克隆——把你今天在 Debug 输出里看到的结构逐字段讲透。
- 之后 u1-l3 会讲 `SEED` / `ITERATIONS` / `OPERATIONS` 环境变量如何复现 `test_random` 的随机化对拍测试,值得先跑一遍留个印象。
- 想提前感受"真实用户"怎么用 sum_tree,可以浏览 [crates/rope/src/rope.rs](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/rope/src/rope.rs) 中 `SumTree<Chunk>` 的定义与查询(完整分析在第 5 单元 u5-l2)。
