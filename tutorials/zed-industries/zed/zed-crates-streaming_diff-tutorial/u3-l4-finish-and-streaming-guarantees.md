# finish 与流式语义：增量差异的正确性保证

## 1. 本讲目标

前三讲我们走完了 `StreamingDiff` 的核心链路：构造与 DP 初始化（u3-l1）、`push_new` 的矩阵滚动与增量填表（u3-l2）、终点搜索与 `backtrack` 回溯（u3-l3）。本讲为单元三收尾，学完后你应当能够：

1. 说出 `finish` 与 `push_new` 返回值在**锚点选择**上的差别：`push_new` 在旧文本侧取「分数最大的自由终点」，`finish` 把终点**双侧钉死**在 \((m, n)\)（旧文本末尾 × 新文本末尾）。
2. 解释 `finish` 在当前实现下为什么会退化为「一段纯删除」，以及这为什么恰好等价于「结算悬挂的旧文本尾部」，并让它满足守恒律。
3. 准确陈述流式使用的**核心不变量**：任意合法分块方式下，把所有 `push_new` 与 `finish` 返回的 `CharOperation` 按序拼接应用到旧文本，都能重建新文本；同时说出哪些性质**会**随分块方式改变。
4. 分析内存与时间复杂度随旧文本长度、块大小、新文本总长的增长边界，理解「流式化节省内存、但不节省总时间」这一取舍。

## 2. 前置知识

本讲默认你已从前三讲建立了以下认知，这里只做一句话回顾：

- **锚点**：`StreamingDiff` 内部维护两个游标 `old_text_ix` / `new_text_ix`（[src/streaming_diff.rs:L118-L119](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L118-L119)），标记「已经被返回的操作结算过的区域」。它们单调不减。
- **自由终点与悬挂尾部**：`push_new` 每轮在最后一列里从 `old_text_ix` 出发找分数最大的行作为新锚点（u3-l3）。被跳过的旧字符（锚点之后的尾部）既没有被 Keep 也没有被 Delete，处于「挂起」状态，等待未来的块或 `finish` 结算。
- **backtrack 是通用的**：给它任意终点 \((i, j)\)，它从 \((i, j)\) 反向走回当前锚点 \((old\_text\_ix, new\_text\_ix)\)，途中用「前驱格分数取最大」选择路径，并输出紧凑化的 `CharOperation` 序列（u3-l3）。分数只影响 hunk 质量，不影响重建正确性。
- **字符边界**：crate 按 `char` 比较、按字节计量（`len_utf8`），因此对 `&str` 切块时必须落在字符边界上。
- **守恒律**（u1-l2）：一段完整的操作序列里，`Keep + Delete` 的字节数等于旧文本长度；`Keep + Insert` 的字节数等于新文本长度。
- **两个 finish 不要混淆**：`StreamingDiff::finish`（本讲主角，[L276-L278](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L276-L278)）与 `LineDiff::finish`（[L453-L460](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L453-L460)）是两个不同类型上的同名方法，后者属于单元四的内容。

## 3. 本讲源码地图

本讲的源码集中在单个文件里，外加基准与一个真实调用方作参照：

| 文件 | 行段 | 作用 |
| --- | --- | --- |
| `crates/streaming_diff/src/streaming_diff.rs` | [L113-L122](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L113-L122) | `StreamingDiff` 结构体：旧/新字符缓冲、打分矩阵、两个锚点、相等游程双缓冲 |
| 同上 | [L149-L199](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L149-L199) | `push_new`：滚动 + 填表 + 自由终点搜索 + 增量回溯（对照用） |
| 同上 | [L184-L197](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L184-L197) | `push_new` 末段：终点搜索与锚点推进（与本讲对照的核心） |
| 同上 | [L201-L274](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L201-L274) | `backtrack`：通用回溯，finish 复用它 |
| 同上 | [L276-L278](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L276-L278) | `StreamingDiff::finish`：本讲主角，仅 3 行 |
| 同上 | [L926-L951](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L926-L951) | `test_random_diffs`：随机化总验收（字符级 + 行级双 round-trip） |
| 同上 | [L963-L981](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L963-L981) | `random_streaming_diff`：随机分块驱动 `push_new` + `finish` 的参考用法 |
| 同上 | [L1104-L1124](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L1104-L1124) | `apply_char_operations`：字符操作的「参考解释器」 |
| `crates/streaming_diff/benches/streaming_diff.rs` | [L8](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/benches/streaming_diff.rs#L8)、[L46-L81](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/benches/streaming_diff.rs#L46-L81) | 512 字节固定分块的基准；`finish` 有独立基准组；fixture 保持较小的原因注释 |
| `crates/agent/src/tools/edit_session.rs` | [L654-L659](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/agent/src/tools/edit_session.rs#L654-L659) | 真实调用方：流结束事件里「最后一块 push_new + finish 拼接」的落地写法 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. **finish：把终点钉死在右下角**——方法本身与它和 `push_new` 终点选择的对照。
2. **悬挂尾部的结算**——为什么 finish 退化为纯删除路径，以及守恒律如何被补齐。
3. **分块一致性不变量**——`random_streaming_diff` 验证的不变量是什么、为什么成立、什么会变。
4. **复杂度与内存边界**——时间 \(\Theta(m \cdot n)\)、矩阵内存 \(\Theta(m \cdot k)\) 的量化分析。

### 4.1 模块一：finish——把终点钉死在右下角

#### 4.1.1 概念说明

流式使用 `StreamingDiff` 的生命周期是三段式：

```text
StreamingDiff::new(old)          // 固定旧文本，初始化第 0 列
    ↓
push_new(chunk_1) → Vec<CharOperation>   // 每到达一块新文本，返回这一段的增量差异
push_new(chunk_2) → Vec<CharOperation>
...
    ↓
finish() → Vec<CharOperation>    // 流结束：结算一切尚未结算的内容
```

`finish` 解决的问题是：**新文本已经全部到齐，不能再有「等下一块再说」的余地**。回顾 u3-l3：`push_new` 每轮的终点搜索是「半全局」的——新文本侧无条件推进到当前末尾，旧文本侧却在最后一列里挑分数最大的行，对不上号的旧尾部零成本挂起。这种方式在流式过程中是合理的（旧尾部也许还能和未来的新字符匹配上），但当流结束时，挂起必须有个了断：`finish` 把回溯终点**强制钉死**在 DP 表的右下角 \((m, n)\)——旧文本最后一个字符之后、新文本最后一个字符之后——让 `backtrack` 把从当前锚点到右下角之间的所有账目一次性结清。

注意签名 `pub fn finish(self)`：它**按值消费** `self`。这在类型层面表达了「finish 是终结操作」——finish 之后再想 `push_new` 会直接编译错误，因为所有权已经移走。真实调用方也呼应了这个语义（见 4.1.3 末尾）。

#### 4.1.2 核心流程

`finish` 与 `push_new` 末段的终点选择对比如下：

| | `push_new` 的终点 | `finish` 的终点 |
| --- | --- | --- |
| 旧文本侧行号 \(i\) | 最后一列中分数最大的行（\(\ge old\_text\_ix\)，并列取最上行） | 强制 \(i = m\)（`self.old.len()`） |
| 新文本侧列号 \(j\) | 当前已到齐的新文本末尾 \(j = n\) | 强制 \(j = n\)（`self.new.len()`） |
| 选择依据 | 打分最大（自由终点） | 无选择，钉死（强制终点） |
| 锚点是否更新 | 是，回溯后推进两个锚点 | 否，`self` 被消费 |
| 未消费的旧尾部 | 继续挂起 | 本次全部结算 |

`finish` 的执行流程可以写成一行伪代码：

```text
finish(self):
    return backtrack(终点 = (m, n), 起点 = 当前锚点 (old_text_ix, new_text_ix))
    # 不填表、不 swap、不 resize——只复用上一次 push_new 留下的矩阵做回溯
```

#### 4.1.3 源码精读

`finish` 的全部实现只有三行（说是两行也不为过）：

[src/streaming_diff.rs:L276-L278](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L276-L278)——`finish` 按值拿走 `self`，直接调用 `self.backtrack(self.old.len(), self.new.len())`：把回溯终点定为旧文本末尾 × 新文本末尾的右下角，从当前锚点反向走回起点的逻辑完全复用 `backtrack`。

对照 `push_new` 的终点搜索：

[src/streaming_diff.rs:L184-L193](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L184-L193)——`push_new` 在最后一列（`next_new_text_ix - self.new_text_ix` 是相对列号）自 `old_text_ix` 起用严格大于找分数最大的行，`next_old_text_ix` 初值就是当前 `old_text_ix`（找不到更好的就原地不动，尾部继续挂起）。

[src/streaming_diff.rs:L195-L197](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L195-L197)——顺序是先回溯、后推进锚点（u3-l3 强调过：`backtrack` 的循环条件依赖旧锚点作为起点）。

真实调用方的落地写法（agent 编辑会话，流结束事件）：

[crates/agent/src/tools/edit_session.rs:L654-L659](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/agent/src/tools/edit_session.rs#L654-L659)——最后一块文本先 `push_new`，再把 `streaming_diff.finish()` 的返回值 `extend` 进同一批 `char_ops` 一起应用。这正是「增量操作 + 收尾操作按序拼接」的标准用法，与下一模块 `random_streaming_diff` 的模式一字不差。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 `push_new` 挂起旧尾部、`finish` 把它结算为删除。

**操作步骤**（示例代码，建议放在本地克隆的 `mod tests` 里运行，练习用途、不要提交）：

```rust
// 示例代码：观察 finish 结算悬挂尾部
#[test]
fn test_observe_finish_settles_tail() {
    let mut diff = StreamingDiff::new("aaaa".to_string());
    let incremental = diff.push_new("aa");
    println!("push_new(\"aa\") -> {:?}", incremental);
    let final_ops = diff.finish();
    println!("finish()         -> {:?}", final_ops);
}
```

运行：`cargo test -p streaming_diff test_observe_finish_settles_tail -- --nocapture`

**需要观察的现象**：两次打印分别是增量操作和收尾操作；新文本只有 "aa"，而旧文本有 4 个 `a`。

**预期结果**（笔者手推 DP 表所得，待本地验证）：`push_new("aa")` 返回 `[Keep { bytes: 2 }]`（第 2 列最大分 2 出现在 \(i=2\) 行，后两个 `a` 挂起）；`finish()` 返回 `[Delete { bytes: 2 }]`（把挂起的两个 `a` 一次删掉）。两者拼接应用到 "aaaa" 恰好得到 "aa"。

#### 4.1.5 小练习与答案

**练习 1**：`finish` 的签名是 `pub fn finish(self)` 而不是 `&mut self`，这带来了什么保证？

**答案**：按值消费所有权后，调用方无法再对同一个 `StreamingDiff` 调用 `push_new`——「finish 之后再推送新文本」这种语义上未定义的用法在编译期就被禁止了。同时它也向调用方明示：diff 的生命周期到此结束。

**练习 2**：`push_new` 与 `finish` 的返回值在锚点选择上的差别，一句话概括？

**答案**：`push_new` 的新侧锚点钉在当前已到齐的新文本末尾、旧侧锚点取最后一列分数最大的行（自由终点，允许旧尾部挂起）；`finish` 把终点双侧钉死在 \((m, n)\)，强制结清所有挂起账目。

**练习 3**：如果不调用 `finish`、直接丢弃 diff，把已有的 `push_new` 操作应用到旧文本会发生什么？

**答案**：对参考解释器 `apply_char_operations` 而言，结果通常仍等于「已推送的那部分新文本」——因为未被 Keep/Delete 覆盖的旧尾部会被它隐式忽略。但守恒律被破坏了（`Keep + Delete` 字节数小于旧文本长度），任何需要精确推进旧文本游标的下游（如 `LineDiff`、真实缓冲区编辑）都拿不到完整的账目。这正是 `finish` 存在的理由：把「隐式忽略」变成「显式删除」。

### 4.2 模块二：悬挂尾部的结算——finish 为何退化为纯删除

#### 4.2.1 概念说明

初看 `backtrack(self.old.len(), self.new.len())`，你可能以为 finish 会做一次「正常的」斜向回溯——像 `push_new` 里那样有插入、删除、相等三种走法竞争。但仔细读循环条件会发现：在当前实现下，**finish 的回溯路径只可能包含删除步**。钥匙是一个不起眼的不变量：

> **不变量 A**：任何时刻 `self.new_text_ix == self.new.len()`。

- `new()` 里两者都是 0（新文本为空）；
- 每次 `push_new` 结束时 `new_text_ix` 被赋值为 `self.new.len()`（[L196-L197](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L196-L197)）；
- `new` 只在 `push_new` 里增长。

于是 `finish` 的回溯起点 \(j = n\) 恰好等于锚点 \(new\_text\_ix\)。而 `backtrack` 里插入步和相等步的前置条件都是 `j > self.new_text_ix`——从头到尾不可能成立（删除步不改变 \(j\)）。所以整条路径是从 \(i = m\) 垂直下行到 \(i = old\_text\_ix\) 的纯删除，配合合并逻辑输出**一个** `Delete` 操作，字节数等于悬挂旧尾部的总字节数。

这正好就是「结算悬挂尾部」的语义：自由终点当初决定不再消费的旧字符，如今流已结束，一律判为删除。反过来讲，如果当初锚点已经推到 \(m\)（没有悬挂），finish 的循环一次都不进，返回空 `Vec`。

#### 4.2.2 核心流程

```text
finish 的回溯（j 恒等于 new_text_ix）:
    i 从 m 下行到 old_text_ix:
        插入候选:  需要 j > new_text_ix  → 恒 false → None
        相等候选:  需要 j > new_text_ix  → 恒 false → None
        删除候选:  需要 i > old_text_ix  → 成立     → Some((i-1, j))
        ⇒ 唯一候选是删除，分数比较形同虚设
        输出: Delete{ bytes: len_utf8(old[i-1]) }，与上一个 Delete 合并
    最终返回: [] 或 [Delete { bytes: 悬挂尾部总字节数 }]
```

用上一节的例子走一遍：旧 "aaaa"、新 "aa"，锚点 \((2, 2)\)。finish 从 \((4, 2)\) 走到 \((2, 2)\)：两步全是删除，输出 `[Delete { bytes: 2 }]`。

守恒律视角：finish 之前 `Keep + Delete = 2 ≠ 4`；finish 之后 `2 + 2 = 4` ✓。同时 `Keep + Insert = 2 + 0 = 2 =` 新文本长度 ✓。**finish 是守恒律的最后一道工序**。

#### 4.2.3 源码精读

候选构造与循环条件：

[src/streaming_diff.rs:L206-L211](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L206-L211)——循环走到当前锚点为止；插入候选要求 `j > self.new_text_ix`。不变量 A 使它在 finish 中恒为 `None`。

[src/streaming_diff.rs:L212-L225](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L212-L225)——删除候选只要求 `i > self.old_text_ix`（悬挂存在时成立）；相等候选同样要求 `j > self.new_text_ix`，在 finish 中恒为 `None`。

[src/streaming_diff.rs:L227-L233](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L227-L233)——`max_by_key` 上 `Some(_)` 恒大于 `None`，所以只要删除候选存在它必然胜出，读到的分数值从不影响决策。

删除合并：

[src/streaming_diff.rs:L248-L259](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L248-L259)——连续的垂直步通过 `hunks.last_mut()` 就地累加字节数（注意 `len_utf8`：按字符走、按字节记），整段悬挂尾部折叠成一个 `Delete`。

> **深入观察（选读）**：finish 期间读的分数列存在「错位」。回溯读分数用的是相对列号 `j - self.new_text_ix`（[L230](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L230)），在 `push_new` 内部调用时 `new_text_ix` 还是旧锚点、列 0 恰好对应它，映射是对的；但 finish 时 `new_text_ix` 已是最新锚点，\(j - new\_text\_ix = 0\) 读到的是列 0——而列 0 存的是**上一次 push_new 开始时**的锚点列（swap 搬进来的那一列）。以 4.1.4 的例子说：finish 在 \((4,2)\) 处读 `get(3, 0) = -60`，而格 \((3,2)\) 的真实分数是 `-18`。这个错位**无害**，因为每一步唯一候选是删除、分数从不参与决策——这也是 u3-l3 结论「正确性由路径合法性保证，分数只影响 hunk 质量」的极端例证：finish 的路径是被循环条件**强制**的，分数完全退场。

#### 4.2.4 代码实践

**实践目标**：验证「finish 补齐守恒律」。

**操作步骤**（示例代码，同样放本地 `mod tests`）：

```rust
// 示例代码：有/无 finish 的守恒律对照
#[test]
fn test_conservation_with_and_without_finish() {
    let count = |ops: &[CharOperation], variant: fn(&CharOperation) -> Option<usize>| {
        ops.iter().filter_map(variant).sum::<usize>()
    };
    let keep_delete = |op: &CharOperation| match op {
        CharOperation::Keep { bytes } | CharOperation::Delete { bytes } => Some(*bytes),
        _ => None,
    };

    let mut diff = StreamingDiff::new("aaaa".to_string());
    let mut ops = diff.push_new("aa");
    assert_eq!(count(&ops, keep_delete), 2); // 未 finish：Keep+Delete < 旧文本长度

    ops.extend(diff.finish());
    assert_eq!(count(&ops, keep_delete), 4); // finish 后守恒律成立
}
```

**需要观察的现象**：第一个断言通过说明「没有 finish 时旧文本有 2 个字节没人认领」；第二个断言通过说明 finish 认领了它们。

**预期结果**：两个断言都通过（与 4.1.4 的手推一致；待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：用不变量 A 论证 finish 的回溯路径只含删除步。

**答案**：不变量 A 说 `new_text_ix == new.len()` 恒成立。finish 的回溯起点 \(j = new.len() = new\_text\_ix\)；插入步与相等步都要求 `j > new_text_ix` 才可用，而唯一的移动方式「删除」不改变 \(j\)，所以 \(j\) 全程钉在 \(new\_text\_ix\)，两种候选永远不可用，路径只能是 \(i\) 从 \(m\) 垂直降到 \(old\_text\_ix\) 的删除序列。

**练习 2**：悬挂的旧尾部里会不会含有「其实能与新文本匹配」的字符？finish 如何处置它们？

**答案**：会。自由终点只是说「继续消费它们在打分上不划算」（例如要付出连续删除的高昂代价），并不代表这些字符在新文本中不存在。finish 不再权衡，一律结算为删除——打分模型在 `push_new` 期间已经做过取舍，finish 只负责结账。

**练习 3**：一段长度为 \(d\) 的悬挂尾部，finish 会输出几个操作？为什么？

**答案**：恰好一个 `Delete`（若 \(d = 0\) 则输出空）。因为整条回溯路径是连续的垂直删除步，而删除分支会把新步合并进 `hunks.last_mut()` 的最后一个 `Delete`。

### 4.3 模块三：分块一致性不变量——random_streaming_diff 的验证

#### 4.3.1 概念说明

流式 API 最让人不放心的地方是：「我一块一块地喂，喂法不同会不会得到不同的答案？」本模块给出 crate 对这个问题的正式回答，也就是它的**核心不变量**：

> **不变量 B（分块一致性）**：设旧文本 `old` 固定，新文本 `new` 以任意方式切成按序的合法字符边界块 \(c_1, c_2, \dots, c_t\)（拼接恰好等于 `new`）。把每次 `push_new(c_i)` 的返回值与最后 `finish()` 的返回值按序拼接成一个操作序列，则把它应用到 `old` 上得到的结果恒等于 `new`。

注意不变量 B 说的是「**重建结果**与分块无关」，而不是「操作序列与分块无关」——后者并不成立（见 4.3.2 末尾）。crate 用随机化测试 `test_random_diffs` 对这一点做了长期回归验证，而 `random_streaming_diff` 是测试里驱动流式 diff 的参考实现，也是使用这个 crate 的标准范式。

#### 4.3.2 核心流程

不变量 B 的论证骨架是**覆盖论证**，分两半：

- **新文本全覆盖**：每次 `push_new` 的回溯从 \((r, J)\) 走到 \((O, A)\)（\(J\) = 本块结束后的新文本末尾，\(A\) = 旧锚点）。路径每一步让 \(j\) 减一，对应恰好一个新字符：插入步的字符进入 `pending_insert` 区间（最终变成 `Insert` 的文本），相等步的字符与某个旧字符配对（由 `Keep` 承载，且 Keep 的旧字节与该字符字节相同——相等步只在字符相等时走）。所以每块返回的操作把新区间 \([A, J]\) 的每个字符**不重不漏**地表达为 `Insert` 文本或 `Keep` 匹配。各块的 \(A\) 正是上一块的 \(J\)，最后 `finish` 补上 \(j\) 方向没有余额（不变量 A），于是新文本 \([0, n)\) 全覆盖。
- **旧文本全覆盖**：同理，每步 \(i\) 减一对应一个旧字符，表达为 `Delete` 或 `Keep`；各块覆盖 \([O_{prev}, r)\)，锚点单调衔接；finish 把残余的 \([old\_text\_ix, m)\) 全部删掉。于是旧文本 \([0, m)\) 也全覆盖——这就是守恒律的来源。

至于「为什么分块不破坏递推」：DP 每列只依赖前一列的分数与相等游程，`push_new` 用 `swap_columns` 把锚点列搬运到列 0 作为下一块的边界列（u3-l2），数学上与一次性填完整张表**逐列等价**。分块改变的只是每块末列上的锚点选择，而不是任何一列的数值。

**什么会随分块改变**：锚点推进的节奏、每次返回的操作数量与切分位置（hunk 形状）、相等游程在块边界的连续性（游程随锚点列搬运，锚点行不同则跨块续算的起点不同，进而影响相等加成与路径选择）。这些都会让**中间过程**的操作序列互不相同——但拼接后的重建结果不变。一句话：**分块影响 diff 的「长相」，不影响 diff 的「正确性」**。

#### 4.3.3 源码精读

[src/streaming_diff.rs:L963-L977](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L963-L977)——`random_streaming_diff` 的流式循环：构造 diff、`new_len` 记录已推送字节数；随机取块长后，[L970-L972](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L970-L972) 把块长逐步加一直到落在字符边界上（随机字节长度可能切在多字节字符内部，`&str` 切片必须落在边界否则 panic——这是本 crate 流式 API 的输入契约）；每块 `push_new` 的返回值直接 `extend` 进累积序列。

[src/streaming_diff.rs:L979](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L979)——循环结束后 `diff.finish()` 的返回值同样 `extend` 进去：**收尾操作与增量操作共用一个序列**，这就是不变量 B 里「按序拼接」的具体形态。

验收断言在随机测试里：

[src/streaming_diff.rs:L938-L949](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L938-L949)——先用 `apply_char_operations` 验证字符级 round-trip（不变量 B），再经 `char_ops_to_line_ops` 用 `apply_line_operations` 验证行级 round-trip（u1-l2 介绍的双重验证链）。

[src/streaming_diff.rs:L1104-L1124](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L1104-L1124)——`apply_char_operations`：`Keep` 拷旧文本切片、`Delete` 只推进游标、`Insert` 追加文本。注意它对「未被覆盖的旧尾部」是隐式忽略的（`old_ix` 只被 Keep/Delete 推进）——这正是 4.1.5 练习 3 里「不 finish 也能碰巧对」的原因。

基准侧的对照：`benches/streaming_diff.rs` 的 [chunk_text（L306-L318）](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/benches/streaming_diff.rs#L306-L318)用了同样的「块长对齐到字符边界」手法，只是改成固定 512 字节上限（[L8](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/benches/streaming_diff.rs#L8)）。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：把不变量 B 从「随机验证」变成「确定性验证」——同一对文本、四种块大小，断言重建结果一致。

**操作步骤**（示例代码，加入本地克隆的 `mod tests`，与 `apply_char_operations` 同模块才能访问私有助手；练习用途、不要提交）：

```rust
// 示例代码：分块一致性测试——不变量 B 的确定性版本
#[test]
fn test_chunk_size_consistency() {
    let old = "aaaa\nbbbb\ncccc";
    let new = "aaaa\nBBBB\ncccc\ndddd";

    for chunk_size in [1, 2, 4, 8] {
        let mut diff = StreamingDiff::new(old.to_string());
        let mut char_operations = Vec::new();

        // 按「字符数」切块，天然落在字符边界上，
        // 相比 random_streaming_diff 的字节对齐手法更简单
        let new_chars: Vec<char> = new.chars().collect();
        for chunk in new_chars.chunks(chunk_size) {
            let chunk: String = chunk.iter().collect();
            char_operations.extend(diff.push_new(&chunk));
        }
        char_operations.extend(diff.finish());

        let patched = apply_char_operations(old, &char_operations);
        assert_eq!(patched, new, "chunk size: {}", chunk_size);
    }
}
```

运行：`cargo test -p streaming_diff test_chunk_size_consistency -- --nocapture`

**需要观察的现象**：

1. 四种块大小下断言是否全部通过；
2. 在循环里加一行 `println!("chunk_size={:?} ops={:?}", chunk_size, char_operations);`，对比四种块大小产生的**操作序列**——预期它们互不相同或粒度不同（例如块大小为 1 时每步只有极少信息可用，操作更碎），但拼接应用的结果相同。

**预期结果**：四个断言全部通过（这正是 `test_random_diffs` 以随机块长长期验证的不变量的确定性实例；若失败即意味着发现了 bug）。各块大小的具体操作序列差异需本地观察（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`random_streaming_diff` 里 `while !new.is_char_boundary(new_len + chunk_len) { chunk_len += 1; }` 为什么必不可少？

**答案**：随机块长是字节数，可能落在多字节 UTF-8 字符内部；`&new[new_len..new_len + chunk_len]` 要求两个下标都在字符边界上，否则运行时 panic。这段循环把块长向后推到最近的字符边界。它同时示范了流式 API 的输入契约：每个 chunk 必须是合法的字符串切片。（4.3.4 的实践用「按字符切块」规避了同一问题。）

**练习 2**：分块大小改变时，哪些性质保证不变？哪些会变？

**答案**：不变：拼接后应用的结果等于新文本（不变量 B）、`Keep+Delete =` 旧文本字节数与 `Keep+Insert =` 新文本字节数（守恒律）。会变：每块末列的锚点选择、每次返回的操作数量与 hunk 切分、跨块相等游程的续算起点——即中间操作序列的「长相」。

**练习 3**：用覆盖论证说明「每个新字符都会被操作序列表达」。

**答案**：每次回溯从 \((r, J)\) 走到 \((O, A)\)，路径每步使 \(j\) 减一，总共减 \(J - A\) 次，对应新区间 \([A, J]\) 的每个字符恰好一次；插入步的字符进入 `Insert` 的文本区间，相等步的字符由对应的 `Keep` 字节承载（相等步仅在字符相等时可用，字节内容一致）。各块区间首尾相接、finish 补齐余额，故 \([0, n)\) 无遗漏也无重复。

### 4.4 模块四：复杂度与内存边界

#### 4.4.1 概念说明

设旧文本 \(m\) 个字符、新文本总共 \(n\) 个字符、单块最多 \(k\) 个字符、共 \(t\) 块。三个关键结论：

1. **矩阵内存与总新文本长度无关**：`push_new` 开头的 swap + resize 把矩阵尺寸钉在 \((m+1) \times (k+1)\)（u3-l2），所以打分矩阵占用约为 \(8(m+1)(k+1)\) 字节——**流式化省的是内存**。
2. **总时间仍是 \(\Theta(m \cdot n)\)**：每列恰好填一次，所有块加起来填了 \(n\) 列 × \((m+1)\) 行，与一次性算完整表相同——**流式化不省时间**，换来的是每块结束就能拿到增量结果。
3. **finish 几乎免费**：它只回溯不填表，步数等于悬挂长度，至多 \(O(m)\)。基准里甚至为它单独立了一组（见 4.4.3）。

唯一随 \(n\) 增长的内存是 `new: Vec<char>` 缓冲（约 \(4n\) 字节）——它必须保留，因为 `backtrack` 要在回溯结束时才从 `self.new[range]` 物化 `Insert` 的文本（[L243-L245](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L243-L245)）；另有 `old: Vec<char>` 约 \(4m\) 字节与两个 \((m+1)\) 长的 `u32` 游程数组。

#### 4.4.2 核心流程

\[ \text{矩阵内存} \approx 8\,(m+1)(k+1)\ \text{字节},\qquad \text{总填表量} \approx (m+1)\,n\ \text{格} \]

\[ \text{时间} = \Theta(m \cdot n + m \cdot t)\ (\text{每块一次 } O(m) \text{ 终点扫描}），\qquad \text{finish} = O(m - old\_text\_ix) \]

两笔账各记一个换算实例（\(m = 10{,}000\) 字符，全 ASCII，\(k = 512\)）：

- 512 字节分块：\(8 \times 10001 \times 513 \approx 41\,\text{MB}\)；
- 若整个新文本（设 5,000 字符）一次性到达：\(8 \times 10001 \times 5001 \approx 400\,\text{MB}\)。

这就是「块大小直接决定矩阵内存」的直观感受：**内存随 \(k\) 线性增长，与 \(n\) 无关**。

#### 4.4.3 源码精读

[src/streaming_diff.rs:L26-L30](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L26-L30)——`Matrix::resize` 委托 `Vec::resize`：行数不变时扩容等价于右侧补零、旧值原位保留（u2-l1）；缩块时截断但容量不还给操作系统，所以矩阵峰值内存由**最大那块**决定。

[src/streaming_diff.rs:L150-L153](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L150-L153)——每块开头 swap + resize：矩阵列数只与本块长度挂钩（`self.new.len() - self.new_text_ix + 1` = \(k+1\)），这是内存上界 \(\Theta(m \cdot k)\) 的直接出处。

[src/streaming_diff.rs:L155-L182](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/src/streaming_diff.rs#L155-L182)——逐列填表主体：每个格子做常数次比较与一次 `powi`，即每块 \(\Theta(m \cdot k)\)，总计 \(\Theta(m \cdot n)\)。

[benches/streaming_diff.rs:L78-L81](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/benches/streaming_diff.rs#L78-L81)——基准注释明说 fixture 保持得较小，因为 `StreamingDiff` 在「几十 KB 的替换文本」上会变得非常慢：这正是 \(\Theta(m \cdot n)\) 二次增长的工程注脚。

[benches/streaming_diff.rs:L46-L75](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/streaming_diff/benches/streaming_diff.rs#L46-L75)——`streaming_diff_finish` 基准组：先在 setup 里推完全部块，再单独计时 `diff.finish()`，与「finish 是廉价的收尾步骤」的结论互为印证。

#### 4.4.4 代码实践

**实践目标**：把复杂度结论从纸面落到可观测的数字。

**操作步骤**：

1. 纸面计算：旧文本 \(m = 10{,}000\) 字符、块 \(k = 512\) 字符时矩阵约多少字节？若新文本 5,000 字符一次性到达又是多少？（用 4.4.2 的公式）
2. 感受 \(\Theta(m \cdot n)\)：在仓库根目录运行 `ITERATIONS=5 OLD_TEXT_LEN=2000 cargo test -p streaming_diff test_random_diffs`，再换成 `OLD_TEXT_LEN=4000` 对比耗时变化（长度翻倍、耗时约翻四倍）。
3. （可选，本地练习、不要提交）在 `push_new` 的 resize 之后临时加一行 `eprintln!("matrix bytes: {}", self.scores.cells.len() * 8);`，用不同块大小推送观察峰值。

**需要观察的现象**：步骤 2 中耗时随 \(m\) 的超线性增长；步骤 3 中矩阵字节数随块大小（而非新文本总长）变化。

**预期结果**：矩阵约 41 MB 对 400 MB（步骤 1）；耗时会明显按平方级扩大，具体数字待本地验证（步骤 2）；矩阵字节数 = \(8(m+1)(k_{\max}+1)\)（步骤 3，待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：流式化（分块推送 vs 一次性推送）节省的是时间还是内存？分别说明量级。

**答案**：内存。总时间都是 \(\Theta(m \cdot n)\)（每列都要填一遍）；矩阵内存从一次性推送的 \(\Theta(m \cdot n)\) 降到 \(\Theta(m \cdot k)\)，\(k\) 是最大块长。额外收益是每块结束即产出增量操作，副作用是 `new` 缓冲仍随 \(n\) 线性保留（约 \(4n\) 字节）。

**练习 2**：`finish` 自身的复杂度是多少？为什么？

**答案**：\(O(m - old\_text\_ix)\)，即悬挂尾部长度，上界 \(O(m)\)。它只复用上一次 `push_new` 留下的矩阵做回溯，不填任何新格子；由不变量 A，回溯还是一条纯垂直路径。

**练习 3**：为什么说矩阵的峰值内存由「最大那块」决定？

**答案**：`Matrix::resize` 底层是 `Vec::resize`，扩容只增不减（缩块时截断长度但保留容量）。某一块把矩阵撑到 \((m+1)(k_{\max}+1)\) 后，后续更小的块不会让容量回落，故峰值由 \(k_{\max}\) 决定。

## 5. 综合实践

把本讲三个模块串成一个任务：写一个「分块一致性 + 守恒律」双断言测试（示例代码，放本地克隆的 `mod tests`，练习用途、不要提交）：

```rust
// 示例代码：综合实践——不变量 B + 守恒律的参数化验证
#[test]
fn test_streaming_guarantees_across_chunk_sizes() {
    let old = "aaaa\nbbbb\ncccc\ndddd";
    let new = "AAAA\nbbbb\ncccc\ndddd\nEEEE";

    for chunk_size in [1, 2, 4, 8] {
        let mut diff = StreamingDiff::new(old.to_string());
        let mut ops = Vec::new();
        for chunk in new.chars().collect::<Vec<_>>().chunks(chunk_size) {
            let chunk: String = chunk.iter().collect();
            ops.extend(diff.push_new(&chunk));
        }
        ops.extend(diff.finish());

        // 不变量 B：任意分块下，拼接应用的结果恒等于新文本
        assert_eq!(apply_char_operations(old, &ops), new);

        // 守恒律：Keep+Delete 覆盖旧文本全部字节；Keep+Insert 覆盖新文本全部字节
        let (mut old_bytes, mut new_bytes) = (0, 0);
        for op in &ops {
            match op {
                CharOperation::Keep { bytes } => {
                    old_bytes += bytes;
                    new_bytes += bytes;
                }
                CharOperation::Delete { bytes } => old_bytes += bytes,
                CharOperation::Insert { text } => new_bytes += text.len(),
            }
        }
        assert_eq!(old_bytes, old.len(), "chunk size: {}", chunk_size);
        assert_eq!(new_bytes, new.len(), "chunk size: {}", chunk_size);

        println!("chunk_size={} ops={:?}", chunk_size, ops);
    }
}
```

要点：

1. `Keep` 同时计入两侧（Keep 的旧字节与新字符一一配对且字节相同）；`Delete` 只计旧侧、`Insert` 只计新侧——这正是覆盖论证的守恒形式。
2. 用 `-- --nocapture` 运行，对比四种块大小打印出的操作序列：粒度和切分位置不同，但两条断言全部成立。
3. 把 `old`/`new` 换成更长的文本（甚至用 `random_text` 生成）重复实验，体会「分块改变长相、不改变正确性」。

预期：所有断言通过；操作序列随块大小变化的具体形态待本地观察。

## 6. 本讲小结

- `finish(self)` 把回溯终点**双侧钉死**在 DP 表右下角 \((m, n)\)，与 `push_new` 的「旧侧自由终点 + 尾部挂起」形成对照；按值消费 `self` 在类型层面禁止 finish 后继续推送。
- 由不变量 A（`new_text_ix == new.len()` 恒成立），finish 的回溯退化为从 \(m\) 垂直下行到 `old_text_ix` 的**纯删除路径**，输出为空或单个 `Delete`——这正是「结算悬挂尾部」的语义；期间读到的错位列分数因唯一候选是删除而从不参与决策。
- finish 把未被消费的旧尾部从「被解释器隐式忽略」变成「显式删除」，补齐守恒律：`Keep+Delete =` 旧文本字节数、`Keep+Insert =` 新文本字节数。
- 核心不变量 B：任意合法字符边界分块下，`push_new` 各次返回值与 `finish` 返回值按序拼接应用到旧文本，恒重建新文本；分块只改变 hunk 形状（锚点节奏、游程续算），不改变重建结果。`random_streaming_diff` + `test_random_diffs` 是它的随机化回归验证。
- 复杂度边界：总时间 \(\Theta(m \cdot n)\)（流式不省时间），矩阵内存 \(\Theta(m \cdot k)\)（流式省内存，峰值由最大块决定），`new` 缓冲 \(4n\) 字节是唯一随总长增长的状态；finish 本身 \(O(m)\) 以内。

## 7. 下一步学习建议

单元三到此完整闭环：构造 → 推流 → 回溯 → 收尾。接下来两条路：

1. **主线——单元四 u4-l1「LineDiff 的状态模型」**：字符操作生产出来之后由谁消费？`LineDiff` 用 `old_end`/`new_end` 两个 rope `Point` 游标和 `deleted_rows`/`inserted_rows` 两个 `BTreeSet` 把字符操作折叠成行操作。理解它为什么**依赖**本讲的守恒律（`Keep+Delete` 必须覆盖旧文本全部字节，游标才能走到位），会反过来加深你对 finish 必要性的认识。
2. **源码延伸**：对照真实调用方 `crates/agent/src/tools/edit_session.rs`（[L592-L627](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/agent/src/tools/edit_session.rs#L592-L627) 的逐块事件与 [L654-L659](https://github.com/zed-industries-zed/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/agent/src/tools/edit_session.rs#L654-L659) 的收尾事件）看一遍「LLM 流式输出 → push_new → 应用到真实缓冲区 → finish 结账」的完整链路；其接入模式的详细拆解在 u4-l5。
