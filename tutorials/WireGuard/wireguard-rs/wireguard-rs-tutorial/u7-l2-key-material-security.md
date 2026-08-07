# 密钥材料的清零与安全

## 1. 本讲目标

本讲聚焦 WireGuard 握手过程中**密钥材料的生命周期终止**——即「不再需要的密钥如何被安全地从内存中抹除」。

读完本讲，你应当能够：

1. 说清**为什么**必须主动清零密钥材料（Drop 不会自动清零栈与堆上的明文密钥），并理解「内存残响」（memory residue）这一威胁。
2. 看懂 [`types.rs`](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/types.rs) 中 `Key::Drop` 如何在析构时把 32 字节密钥清零，以及 `KeyPair` 如何借「字段级 Drop」级联清零收发密钥。
3. 掌握 [`noise.rs`](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs) 中 `clear_stack_on_return_fnone(CLEAR_PAGES, …)` 的用途——在函数返回时强制擦除栈帧上残留的敏感中间量。
4. 理解 `State::Drop` 如何清除握手转录（`hs`/`ck`），并指出**临时私钥 `eph_sk` 由谁负责清零**（提示：不是本仓库）。
5. 看懂 `shared_secret` 对「零共享密钥」的拒绝，以及 `subtle::ct_eq` 常时间比较为何能防时序侧信道。

本讲承接 u4-l3（Noise IK 握手核心），把视线从「密钥如何被算出来」转到「密钥如何被安全地抹掉」。

## 2. 前置知识

- **Rust 的 `Drop` 不是「清零」**：`Drop` 只负责释放资源（如归还堆内存），并不会把内存内容写零。一个 `[u8; 32]` 数组在被释放前，它的 32 字节明文会原样留在原地——可能是栈上，也可能是堆块里（堆块归还给分配器后，仍可能被再次分配出来被读到）。所以「敏感数据用完即抹」必须由开发者显式实现。
- **栈帧残响**：函数内的局部变量（如临时密钥、DH 中间值）存在栈上。函数返回后，栈指针上移，但这些字节并没被覆盖，后续任何一次函数调用都可能把它们读出来。`clear_on_drop` crate 提供的 `clear_stack_on_return_fnone` 就是专门解决这个问题的。
- **常时间比较（constant-time comparison）**：普通的 `==` 在发现第一个不匹配字节时就提前返回，执行时长因此泄露了「前缀匹配长度」。`subtle::ConstantTimeEq`（`ct_eq`）无论结果如何都遍历全部字节、用位运算聚合，执行时长与数据无关，可防时序侧信道。本讲中它被用于「判断共享密钥是否为零」。
- **零共享密钥攻击**：Curve25519 上，若对端公钥是「低阶点」（small subgroup），DH 结果可能为零或可预测值。拒绝零共享密钥是与 Linux 内核实现保持一致的防御。
- 你应已读过 u4-l3，了解 Noise 握手中的链密钥 `ck`、转录哈希 `hs`、临时密钥 `eph_sk` 等中间量的含义。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/wireguard/types.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/types.rs) | 定义传输层会话密钥 `Key`/`KeyPair`，是 `Key::Drop` 清零的所在地。 |
| [src/wireguard/handshake/noise.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs) | Noise IK 握手的密码学核心。四个握手函数全部包裹在 `clear_stack_on_return_fnone` 中；`shared_secret` 在此做零值检查。 |
| [src/wireguard/handshake/peer.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/peer.rs) | 定义握手状态机 `State`，`State::Drop` 负责清除跨报文中间量 `hs`/`ck`。 |
| [src/wireguard/handshake/device.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs) | `update_ss` 在私钥变更时清零预计算共享密钥 `peer.ss`，作为「配置变更即清零」的补充例证。 |

依赖方面，本讲涉及两个关键外部 crate：`clear_on_drop = "0.2.3"`（提供 `Clear` trait 与 `clear_stack_on_return_fnone`）、`subtle`（提供 `ConstantTimeEq`）。

## 4. 核心概念与源码讲解

### 4.1 堆上密钥的清零卫士：`Key::Drop`

#### 4.1.1 概念说明

握手成功后，Noise 函数会派生出一对会话密钥（收/发各一），交给路由器用于 ChaCha20-Poly1305 数据面加解密。这对密钥在 [`types.rs`](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/types.rs) 里被建模为 `Key`：

```rust
pub struct Key {
    pub key: [u8; 32],   // 真正的对称密钥明文
    pub id: u32,         // receiver id，非敏感
}
```

`key` 字段是 32 字节的对称密钥明文，是必须保护的核心秘密；`id` 只是路由用的标识符，不敏感。`Key` 通常被包进 `KeyPair`，再被 `Arc<KeyPair>` 共享到多个工作线程（见 u5-l6 的 KeyWheel）。当某个会话密钥过期、被轮换或 peer 被删除时，`Key` 最终会被析构——而我们需要确保那一刻它的 32 字节明文被写零，而不是原样留在一个可能被重新分配出去的堆块里。

#### 4.1.2 核心流程

1. `Key` 离开作用域（或其所在的 `KeyPair`/`Arc` 被释放），Rust 运行时调用 `Key::drop`。
2. `drop` 内部调用 `self.key.clear()`——这是 `clear_on_drop` crate 提供的 `Clear` trait 方法，语义是「用零原地覆写」。
3. **只清 `key`，不清 `id`**：`id` 非敏感，不必也无法在清零上浪费精力；这种「精准清零」也是安全代码的常见取舍。
4. 此后即便堆块被分配器回收并再次分发，读出来的也是零，不再是密钥明文。

#### 4.1.3 源码精读

[src/wireguard/types.rs:11-16](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/types.rs#L11-L16) —— 注释一行道破意图，`Drop` 里只调用 `self.key.clear()`：

```rust
// zero key on drop
impl Drop for Key {
    fn drop(&mut self) {
        self.key.clear()
    }
}
```

注意文件首行 `use clear_on_drop::clear::Clear;`（[types.rs:1](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/types.rs#L1)）把 `.clear()` 方法引入作用域。`Clear` trait 对 `[u8; N]` 的实现就是逐字节写零——它**不依赖**编译器在 release 下「省略 `Drop`」的优化，因为这是手写的 `Drop`，一定会执行。

> 小贴士：为什么不用 `Zeroize`？`clear_on_drop` 与 `zeroize` 思路一致（都用易失写零防止编译器「优化掉」写操作），本项目统一选用前者。

#### 4.1.4 代码实践（源码阅读型）

**目标**：确认 `Key::Drop` 的触发时机与作用范围。

1. 打开 `src/wireguard/types.rs`，阅读 `Key` 与 `impl Drop for Key`。
2. 全仓搜索 `KeyPair` 的消费点（提示：`src/wireguard/router/peer.rs` 的 `EncryptionState::new` / `DecryptionState::new` 从 `Arc<KeyPair>` 取出 `key` 字段做对称加解密）。
3. **观察现象（待本地验证）**：在 `Key::drop` 里临时加一行 `log::trace!("Key {:?} zeroed", self.id);`，编译后跑 `wireguard/tests.rs` 中的 `test_pure_wireguard`。握手成功→定时器到期→密钥轮换时，应当看到旧 `Key` 的「zeroed」日志，证明 Drop 真的触发。
4. **预期结果**：每次密钥轮换（`zero_keys` 或 KeyWheel 轮转）都会产生清零日志；这印证了「密钥生命周期终点 = Drop = 清零」。

> 注意：改完务必撤销这行日志，不要把密钥 id 留在生产日志里。本仓库禁止修改源码作为交付，此处仅作本地验证练习。

#### 4.1.5 小练习与答案

**Q1**：`Key::Drop` 为什么只清 `self.key` 而不清 `self.id`？
**答**：`id`（receiver id）是用于路由的非敏感标识，清零它既无安全收益又徒增开销；安全清零应只针对真正的秘密材料（32 字节对称密钥）。

**Q2**：如果把 `Key` 的 `key` 字段类型从 `[u8; 32]` 改成一个**没有**实现 `Clear` 的自定义类型，会发生什么？
**答**：编译失败——`.clear()` 来自 `clear_on_drop::clear::Clear` trait，类型必须实现该 trait 才能调用。这正是 Rust 类型系统对「清零能力」的静态保证。

---

### 4.2 `KeyPair`：会话密钥容器与级联清零

#### 4.2.1 概念说明

`KeyPair` 把一次成功握手派生出的「发送密钥 + 接收密钥 + 元信息」打包成一个整体，是路由器 KeyWheel（u5-l6）流转的基本单位。它本身**没有**手写 `Drop`，但其两个字段 `send: Key` 与 `recv: Key` 都是 `Key`——而 `Key` 实现了清零型 `Drop`。于是 Rust 的字段级析构会自动触发「级联清零」：`KeyPair` 被释放时，`send` 和 `recv` 各自的 `Key::drop` 被调用，两份密钥同时归零。

#### 4.2.2 核心流程

```
KeyPair 被 drop（例如 Arc 引用计数归零）
        │
        ├─ field send: Key  → 调用 Key::drop → send.key.clear()
        └─ field recv: Key  → 调用 Key::drop → recv.key.clear()
        │
        └─ birth: Instant, initiator: bool → 无 Drop，普通释放
```

关键点：`birth`（创建时刻）和 `initiator`（是否已确认）都不是秘密，无需清零；只有两个 `Key` 字段需要、也确实会被清零。Rust 保证结构体析构时**所有字段按声明顺序依次析构**，所以无需手写 `Drop for KeyPair`。

#### 4.2.3 源码精读

[src/wireguard/types.rs:31-37](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/types.rs#L31-L37) —— 注意没有 `impl Drop for KeyPair`：

```rust
pub struct KeyPair {
    pub birth: Instant,   // 创建时刻（非敏感）
    pub initiator: bool,  // 是否已确认（非敏感）
    pub send: Key,        // ← 析构时清零
    pub recv: Key,        // ← 析构时清零
}
```

`KeyPair` 由 `noise.rs` 在 `create_response` / `consume_response` 末尾构造并返回（[noise.rs:475-486](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L475-L486) 与 [noise.rs:573-584](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L573-L584)），随后被路由器包进 `Arc<KeyPair>` 存入 KeyWheel 的 `current`/`previous`/`next` 槽（u5-l6）。当密钥轮换或 peer 删除时，旧 `Arc<KeyPair>` 引用计数归零，最终触发这里的级联清零。

#### 4.2.4 代码实践（源码阅读型）

**目标**：沿「KeyPair 的诞生→流转→清零」走一遍调用链。

1. 在 `noise.rs` 找到两处 `Ok(KeyPair { … })`（`create_response` 与 `consume_response` 的返回值）。
2. 跟到 `workers.rs` 的 `handshake_worker`，看它把 `KeyPair` 经 `peer.add_keypair` 交给路由器（见 u4-l6）。
3. 在 `router/peer.rs` 找 `zero_keys`（约 [peer.rs:383](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L383)），看它如何让 `current`/`previous`/`next` 槽位置空，从而让旧 `Arc<KeyPair>` 引用计数归零。
4. **预期结果**：你能画出 `KeyPair → Arc → KeyWheel 槽 → 置 None → 引用计数归零 → Key::drop ×2 → 清零` 这条完整的「密钥善终」链路。

#### 4.2.5 小练习与答案

**Q1**：为什么 `KeyPair` 不需要手写 `impl Drop`？
**答**：因为它需要清零的全部内容（`send`/`recv`）都是已实现清零型 `Drop` 的 `Key` 字段；Rust 的字段级析构会自动级联调用它们的 `Drop`，手写反而多余且容易遗漏。

**Q2**：`Arc<KeyPair>` 的引用计数从 2 降到 0 时，`Key::drop` 会被调用几次？
**答**：2 次——`send` 和 `recv` 各一次。`Arc` 内层 `KeyPair` 只在此刻被析构一次，但析构时两个字段各自触发一次 `Key::drop`。

---

### 4.3 栈帧擦除：`clear_stack_on_return_fnone` 与 `CLEAR_PAGES`

#### 4.3.1 概念说明

`Key::Drop` / `State::Drop` 解决的是「有名字、被拥有、存在于堆或结构体里」的密钥。但 Noise 握手函数里还有大量**栈上临时变量**：链密钥 `ck`、转录哈希 `hs`、AEAD 中间密钥 `key`、临时公钥 `eph_pk`、解密出来的对端静态公钥 `pk`……它们中的许多是 `[u8; 32]` 或 `GenericArray`，**没有实现清零型 Drop**（或在本上下文里不会被显式 clear）。函数一返回，这些字节就裸露在栈上，等待后续调用覆盖——但在被覆盖前，它们可被读出。

`clear_on_drop` crate 的 `clear_stack_on_return_fnone(num_pages, closure)` 正是为此而生：它执行闭包，**并在闭包返回时**把当前栈指针下方 `num_pages` 页（每页 4096 字节）强行写零，从而擦除这次调用在栈上留下的敏感残响。名字里的 `fnone` 表示它接受一个 `FnOnce`（一次性）闭包。

#### 4.3.2 核心流程

```
create_initiation / consume_initiation / create_response / consume_response
        │
        │  全部形如：
        │  clear_stack_on_return_fnone(CLEAR_PAGES, || {
        │       …所有敏感计算（ck/hs/key/eph_sk/pk…）…
        │       Ok(返回值)
        │  })
        │
        ▼
闭包返回瞬间：把栈指针下方 CLEAR_PAGES(=1) 页强制写零
        │
        ▼
ck/hs/key/pk 等栈上明文残响被抹除（已 move 进返回值的除外）
```

`CLEAR_PAGES` 取 1（即 4096 字节）是一个**启发式折中**：足够覆盖这些函数实际用到的栈空间，又不过度擦除无关区域。它不是「绝对安全」——理论上若某次调用的栈用量超过一页，溢出部分的残响不会被擦除；这是性能与安全的工程取舍。

#### 4.3.3 源码精读

[src/wireguard/handshake/noise.rs:19-22](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L19-L22) 与 [src/wireguard/handshake/noise.rs:44-45](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L44-L45) —— 导入与页数常量：

```rust
use clear_on_drop::clear::Clear;
use clear_stack_on_return_fnone;
…
use subtle::ConstantTimeEq;
…
// number of pages to clear after sensitive call
const CLEAR_PAGES: usize = 1;
```

四个握手函数无一例外地把整个函数体包进 `clear_stack_on_return_fnone`。以 `create_initiation` 为例：[src/wireguard/handshake/noise.rs:245](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L245)：

```rust
clear_stack_on_return_fnone(CLEAR_PAGES, || {
    let ck = INITIAL_CK;
    let hs = INITIAL_HS;
    …
    let eph_sk = StaticSecret::new(rng);   // 临时私钥
    let (ck, key) = KDF2!(&ck, shared_secret(&eph_sk, &pk)?.as_bytes());
    …
    *peer.state.lock() = State::InitiationSent { hs, ck, eph_sk, local };
    Ok(())
})
```

注意：被 `move` 进返回值或 `State` 的数据（如 `hs`/`ck`/`eph_sk` 进入 `State::InitiationSent`）会随所有者继续存活，由各自的 `Drop` 负责善终（见 4.4）；而留在栈上的中间量（如 AEAD 的 `key`、临时 `eph_pk`）则由 `clear_stack_on_return_fnone` 统一擦除。其余三处包裹点：`consume_initiation`（[noise.rs:326](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L326)）、`create_response`（[noise.rs:415](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L415)）、`consume_response`（[noise.rs:500](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L500)）。

#### 4.3.4 代码实践（源码阅读型）

**目标**：理解 `CLEAR_PAGES` 的局限并定位全部包裹点。

1. 在 `noise.rs` 搜索 `clear_stack_on_return_fnone`，确认四处调用全部对应四个握手函数。
2. 思考：`consume_initiation` 返回的 `TemporaryState` 里携带了 `hs`/`ck`（[noise.rs:398-402](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L398-L402)），它们在 `create_response` 内被消费。问：这两个值在跨越函数边界时，是否仍处在 `clear_stack_on_return_fnone` 的保护伞下？
3. **预期结论（待本地验证/推理）**：`TemporaryState` 是被显式传递的「跨函数中间量」，它的清零依赖 `create_response` 内部的 `clear_stack_on_return_fnone`（返回时擦除其栈副本）——也就是说，保护是「按调用」分别生效的，不是全局的。这正是为何**每个**握手函数都要各自包裹。

#### 4.3.5 小练习与答案

**Q1**：`CLEAR_PAGES = 1` 意味着擦除多少字节？为什么不是 0 或一个很大的数？
**答**：1 页 = 4096 字节。取 0 等于不擦除，失去意义；取过大会擦除大量无关栈区，徒增开销且可能误伤。1 页是覆盖这些握手函数实际栈用量的经验值。

**Q2**：`clear_stack_on_return_fnone` 能否替代 `Key::Drop`？
**答**：不能。前者只擦「当前函数这次调用」在栈上留下的临时量；`Key` 通常活在堆里（被 `Arc` 共享），其生命周期远超任何一次函数调用，只能靠 `Drop` 在真正释放时清零。二者互补，不互相替代。

---

### 4.4 `State::Drop`：清除握手转录，以及 `eph_sk` 的归属

#### 4.4.1 概念说明

Noise 握手是**两报文往返**：发起方发完 Initiation 后，要记住一批中间量（链密钥 `ck`、转录哈希 `hs`、临时私钥 `eph_sk`、本地 receiver id `local`），等到收到 Response 时才能继续推进。这批中间量被存进 `State::InitiationSent`，挂在 peer 的状态机里（见 u4-l5）。

这批中间量里，`hs` 和 `ck` 是 `GenericArray<u8, U32>`（32 字节数组），属于敏感的「握手转录/链密钥」，必须清零；`eph_sk` 是 x25519-dalek 的 `StaticSecret`（临时私钥），同样敏感。`State::Drop` 负责：当状态被重置（`State::Reset`）或 peer 被销毁时，清除这些转录。但**注意**：`eph_sk` 不由本仓库清零——它依赖 x25519-dalek 自身的清零型 `Drop`。

#### 4.4.2 核心流程

```
State::InitiationSent 离开作用域 / 被 mem::replace 成 Reset
        │
        ▼  State::drop 触发
        ├─ hs.clear()   ← 本仓库显式清零
        ├─ ck.clear()   ← 本仓库显式清零
        └─ eph_sk       ← 不在此清零！由 x25519-dalek 的 StaticSecret::drop 自动清零
        └─ local: u32   ← 非敏感，无需清零
```

`State::Drop` 用 `if let State::InitiationSent { hs, ck, .. } = self` 模式匹配，只处理 `InitiationSent` 变体（`Reset` 变体没有任何敏感数据）。`..` 忽略了 `eph_sk` 和 `local`——前者交给 dalek，后者非敏感。

#### 4.4.3 源码精读

[src/wireguard/handshake/peer.rs:41-49](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/peer.rs#L41-L49) —— 状态枚举，`InitiationSent` 携带四个跨报文中间量：

```rust
pub enum State {
    Reset,
    InitiationSent {
        local: u32,                       // 本地 receiver id（非敏感）
        eph_sk: StaticSecret,             // 临时私钥（由 dalek 清零）
        hs: GenericArray<u8, U32>,        // 转录哈希（本仓库清零）
        ck: GenericArray<u8, U32>,        // 链密钥（本仓库清零）
    },
}
```

[src/wireguard/handshake/peer.rs:51-59](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/peer.rs#L51-L59) —— 关键注释点明 `eph_sk` 的清零归属：

```rust
impl Drop for State {
    fn drop(&mut self) {
        if let State::InitiationSent { hs, ck, .. } = self {
            // eph_sk already cleared by dalek-x25519
            hs.clear();
            ck.clear();
        }
    }
}
```

注释 `// eph_sk already cleared by dalek-x25519` 是本讲的「关键证据」：临时私钥 `eph_sk` 的清零责任不在 wireguard-rs，而在其依赖 `x25519-dalek`——该 crate 的 `StaticSecret` 实现了析构时自动清零（基于 `zeroize` crate）。所以即便这里用 `..` 忽略了它，`eph_sk` 字段被析构时 dalek 的 `Drop` 仍会触发，把它写零。这是一种「把密钥清零责任委托给密码学库」的合理做法。

补充例证——「配置变更即清零」：在 `device.rs` 的 `update_ss` 中，当设备私钥变更或与某 peer 公钥撞车时，预计算共享密钥 `peer.ss` 会被显式 `clear()`：[src/wireguard/handshake/device.rs:106-127](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L106-L127)（关键行 `peer.ss.clear()`）。这说明清零并非只发生在 Drop，配置路径上的敏感数据流转同样贯彻「用完即抹」。

#### 4.4.4 代码实践（源码阅读型）

**目标**：验证 `eph_sk` 的清零依赖关系，理解「委托清零」。

1. 打开 `peer.rs` 阅读 `State` 枚举与 `State::Drop`，留意注释。
2. 在 `noise.rs` 的 `consume_response` 中，看 `eph_sk` 如何被**拷贝**出来用（[noise.rs:504-512](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L504-L512)）：

   ```rust
   let (hs, ck, local, eph_sk) = match *peer.state.lock() {
       State::InitiationSent { hs, ck, local, ref eph_sk } =>
           Ok((hs, ck, local, StaticSecret::from(eph_sk.to_bytes()))),
       …
   }?;
   ```
   这里 `eph_sk.to_bytes()` 取出字节再 `StaticSecret::from(…)` 重建一份副本——原 `State` 里的 `eph_sk` 仍留在 peer 状态里，副本用于本函数计算。两份最终都由 dalek 的 `Drop` 清零。
3. **预期结论**：本仓库代码中**找不到**任何对 `eph_sk` 调用 `.clear()` 的地方——它的清零完全由 `x25519-dalek` 保证。这就是「委托给密码学库」的含义。

#### 4.4.5 小练习与答案

**Q1**：`State::Drop` 里的 `if let … { hs, ck, .. }` 为什么用 `..` 忽略 `eph_sk`，而不是也调用 `eph_sk.clear()`？
**答**：因为 `eph_sk` 是 `StaticSecret` 类型，其清零由 `x25519-dalek` 的 `Drop` 自动完成；重复清零既无必要，`StaticSecret` 也未必暴露 `.clear()` 方法。注释 `// eph_sk already cleared by dalek-x25519` 明确交代了这一委托关系。

**Q2**：若未来把 `x25519-dalek` 换成一个**不**在 Drop 时清零的库，会发生什么安全风险？
**答**：临时私钥 `eph_sk` 的明文会残留在被释放的内存里，可能被再次分配出来读出，从而泄露一次性私钥——虽然临时私钥本身寿命短，但泄露它会破坏该次握手的前向安全性。这正是依赖密码学库「自清零」属性的风险点，审计时需特别确认。

---

### 4.5 共享密钥零检查与 `ct_eq` 常时间比较

#### 4.5.1 概念说明

清零是「密钥死亡时」的防御；本节讲「密钥出生时」的一道关卡——**拒绝零共享密钥**。

Curve25519 DH 在某些病态输入下（对端公钥为低阶点）会产生全零或可预测的共享密钥。若放任其进入后续 KDF，会让链密钥退化、握手可被伪造。Noise 规范**并不要求**检查零共享密钥，但 Linux 内核 WireGuard 做了这个检查；wireguard-rs 为追求「与内核绝对等价」而同样实现它（见函数注释）。

实现上有两个要点：

1. **如何判断「全零」**：不能用 `==`（提前返回会泄露前缀匹配长度，构成时序侧信道），而要用 `subtle::ConstantTimeEq` 的 `ct_eq`，它对全部 32 字节做位运算聚合、执行时长与数据无关。
2. **两道关**：`shared_secret` 包装函数检查 DH 结果 `ss` 是否为零；调用侧还额外检查预计算的静态-静态共享密钥 `peer.ss` 是否为零（后者来自配置/私钥变更，正常情况下应已非零，这是一道冗余防御）。

#### 4.5.2 核心流程

```
shared_secret(sk, pk):
    ss = sk.diffie_hellman(pk)
    若 ss.ct_eq([0;32]) 为真 → 返回 Err(InvalidSharedSecret)   ← 拒绝零密钥
    否则 → 返回 Ok(ss)

create_initiation / consume_initiation 入口处另查:
    若 peer.ss.ct_eq([0;32]) 为真 → 返回 Err(InvalidSharedSecret)  ← 冗余防御
```

`ct_eq` 返回的不是 `bool` 而是一个「常时间选择值」，需要 `.into()` 才得到 `bool`——这迫使调用方显式承认「此处退出常时间域」。

#### 4.5.3 源码精读

[src/wireguard/handshake/noise.rs:215-228](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L215-L228) —— `shared_secret` 包装函数，注释解释了「为何做、为谁对齐」：

```rust
// Computes an X25519 shared secret.
// This function wraps dalek to add a zero-check.
// This is not recommended by the Noise specification,
// but implemented in the kernel with which we strive for absolute equivalent behavior.
#[inline(always)]
fn shared_secret(sk: &StaticSecret, pk: &PublicKey) -> Result<SharedSecret, HandshakeError> {
    let ss = sk.diffie_hellman(pk);
    if ss.as_bytes().ct_eq(&[0u8; 32]).into() {
        Err(HandshakeError::InvalidSharedSecret)
    } else {
        Ok(ss)
    }
}
```

调用侧的冗余检查——`create_initiation`（[noise.rs:240-243](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L240-L243)）与 `consume_initiation`（[noise.rs:359-363](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L359-L363)）：

```rust
// check for zero shared-secret (see "shared_secret" note).
if peer.ss.ct_eq(&[0u8; 32]).into() {
    return Err(HandshakeError::InvalidSharedSecret);
}
```

`ct_eq` 还被用在 `consume_response` 里做**密钥身份比对**——判断收到的 Response 是否仍对应「锁释放前」的那次 Initiation（防止锁释放窗口内并发新 Initiation 后旧 Response 被重放）：[src/wireguard/handshake/noise.rs:556-561](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L556-L561)。这里对临时私钥字节做常时间比较，同样避免时序泄露。

#### 4.5.4 代码实践（源码阅读型）

**目标**：理解 `ct_eq` 相对 `==` 的安全性差异。

1. 读 `shared_secret` 函数，注意 `ss.as_bytes().ct_eq(&[0u8; 32]).into()` 的三段式：`as_bytes()` 取切片 → `ct_eq` 常时间比较 → `.into()` 转为 `bool`。
2. 对比设想：若写成 `if ss.as_bytes() == &[0u8; 32]`，编译器会逐字节短路比较，第一字节非零即返回——攻击者可通过测量响应时间推断「共享密钥前导零的个数」。
3. **预期结论**：`ct_eq` 的核心价值是「执行时长与密钥内容无关」，在比较**秘密**时不可用 `==`；比较非秘密（如版本号）时则无需此顾虑。

> 待本地验证：可写一个微基准，分别测 `ct_eq` 与 `==` 在「首字节相同」与「首字节不同」两种输入下的耗时差异，理论上 `ct_eq` 两者耗时一致，`==` 差异显著。

#### 4.5.5 小练习与答案

**Q1**：为什么 `ct_eq` 的结果要先 `.into()` 成 `bool` 才能用在 `if` 里？
**答**：`ct_eq` 返回的是 `subtle::Choice`（常时间选择值）而非 `bool`，这是为了**防止**你无意中把它放进会短路求值的表达式里（如逻辑运算）。`.into()` 是一次显式的「退出常时间域」操作，提醒开发者此处已接受时序暴露。

**Q2**：`shared_secret` 的零检查「不符合 Noise 规范但与内核对齐」，这种取舍的利弊是什么？
**答**：利是保证 wireguard-rs 与内核实现「行为绝对一致」，便于互操作测试与审计对照；弊是偏离了规范，理论上可能在某些合法但产生零共享密钥的边缘输入下拒绝本应成功的握手。由于 Curve25519 产生零共享密钥只在对端公钥为低阶点（病态/攻击输入）时发生，实际利大于弊。

---

## 5. 综合实践：审计 `noise.rs` 的敏感变量清零归属

本任务把本讲全部知识点串起来，做一次「密钥材料清零审计」——这是阅读密码学代码时最有价值的练习。

### 实践目标

逐个列出 `noise.rs` 四个握手函数中的敏感局部变量，标注每个变量「由谁负责清零」，特别要回答：**临时私钥 `eph_sk` 由谁清零？**

### 操作步骤

1. 打开 [src/wireguard/handshake/noise.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs)，对四个函数 `create_initiation`、`consume_initiation`、`create_response`、`consume_response` 分别列表。
2. 对每个函数，找出其中所有「携带秘密的局部变量」（提示：`ck`、`hs`、`key`、`tau`、`eph_sk`、`eph_pk`、`pk`、`ts` 等字节型变量；忽略 `msg`、`local`、`receiver` 等非敏感量）。
3. 为每个敏感变量标注其清零责任方，从三类中选一：
   - **栈擦除**：`clear_stack_on_return_fnone(CLEAR_PAGES)` 在函数返回时擦除（适用于留在栈上的临时量）。
   - **Drop 清零**：被 `move` 进返回值/`State` 的，由 `Key::Drop` 或 `State::Drop` 清零。
   - **委托清零**：`eph_sk`（`StaticSecret`）由 `x25519-dalek` 自身的 Drop 清零。
4. **回答关键问题**：临时私钥 `eph_sk` 在 `create_initiation` 中被 `move` 进 `State::InitiationSent`；在 `consume_response` 中被 `to_bytes()` 拷贝。它在本仓库代码里**从不**被显式 `.clear()`——它的清零完全由谁负责？

### 需要观察的现象 / 参考答案表

下表给出审计的参考结论（请你先自行完成再对照）：

| 函数 | 敏感变量举例 | 清零责任方 |
| --- | --- | --- |
| `create_initiation` | `ck`、`hs`、`key`、`eph_pk`（栈上） | `clear_stack_on_return_fnone`（栈擦除） |
| `create_initiation` | `hs`、`ck`（move 进 `State`） | `State::Drop` |
| `create_initiation` | `eph_sk`（move 进 `State`） | **`x25519-dalek` 的 `StaticSecret::Drop`**（委托清零，见 peer.rs:54 注释） |
| `consume_initiation` | `ck`、`hs`、`key`、`pk[32]`、`ts` | `clear_stack_on_return_fnone`（栈擦除） |
| `consume_initiation` | `hs`、`ck`（随 `TemporaryState` 返回） | 在 `create_response` 内由 `clear_stack_on_return_fnone` 擦除其副本 |
| `create_response` | `ck`、`hs`、`key`、`tau`、`eph_sk`、`eph_pk` | `clear_stack_on_return_fnone`（栈擦除） |
| `create_response` | 返回的 `KeyPair.send` / `.recv` | `Key::Drop`（最终随 KeyWheel 轮换清零） |
| `consume_response` | `ck`、`hs`、`key`、`tau`、`eph_sk`(副本)、`eph_r_pk` | `clear_stack_on_return_fnone`（栈擦除） |
| `consume_response` | 返回的 `KeyPair.send` / `.recv` | `Key::Drop` |

### 关键问题的答案

**临时私钥 `eph_sk` 由 `x25519-dalek` crate 负责（委托清零）**，本仓库不直接清零它。证据有三：

1. `State::Drop`（[peer.rs:51-59](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/peer.rs#L51-L59)）用 `..` 忽略 `eph_sk`，并注释 `// eph_sk already cleared by dalek-x25519`。
2. 全仓搜索 `.clear()`（见本讲 4.1 节列出的清零点）找不到任何对 `eph_sk` 的调用。
3. `StaticSecret` 是 `x25519-dalek` 提供的类型，该类型在析构时基于 `zeroize` crate 自动清零——这是「把密钥清零责任委托给底层密码学库」的标准做法。

### 预期结果

完成审计后，你应当能用一句话概括 wireguard-rs 的三层密钥清零体系：

> **栈上临时量**由 `clear_stack_on_return_fnone` 擦除；**结构体内的密钥**由 `Key::Drop` / `State::Drop` 清零；**临时私钥 `eph_sk`** 委托给 `x25519-dalek` 清零。三者共同覆盖了「出生（零检查）→ 流转 → 死亡（清零）」的完整密钥生命周期。

## 6. 本讲小结

- Rust 的 `Drop` 不会自动清零内存；密钥材料的「用完即抹」必须显式实现，否则明文会残留在堆块或栈帧里，构成「内存残响」威胁。
- [`Key::Drop`](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/types.rs#L11-L16) 调用 `self.key.clear()` 把 32 字节对称密钥清零；`KeyPair` 无需手写 Drop，靠字段级析构级联清零 `send`/`recv` 两把密钥。
- [`clear_stack_on_return_fnone(CLEAR_PAGES=1, …)`](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L245) 包裹全部四个握手函数，在返回瞬间擦除栈帧上的敏感中间量（`ck`/`hs`/`key` 等），补齐「栈残响」这一 Drop 管不到的盲区。
- [`State::Drop`](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/peer.rs#L51-L59) 清除握手转录 `hs`/`ck`；**临时私钥 `eph_sk` 不由本仓库清零**，而是委托给 `x25519-dalek` 的 `StaticSecret::Drop`（基于 `zeroize`）。
- [`shared_secret`](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/noise.rs#L215-L228) 用 `subtle::ct_eq` 做常时间零检查，拒绝零共享密钥（与内核对齐、抗低阶点攻击）；`ct_eq` 比较秘密时杜绝时序侧信道。
- 整体构成「出生（零检查）→ 流转（KeyWheel）→ 死亡（Drop/栈擦除/委托）」的密钥生命周期安全闭环。

## 7. 下一步学习建议

- **横向对照数据面**：路由器侧的会话密钥清零在 u5-l6（KeyWheel 与 Peer 生命周期）的 `zero_keys` 中实现，可对照本讲理解「握手层 vs 数据面层」的清零分工。
- **深入定时器触发**：清零的实际触发点之一是密钥过期定时器，见 u7-l1（定时器状态机与 Callbacks）的 `zero_key_material` 定时器与 `TIMERS_*` 常量。
- **审计扩展练习**：尝试把本讲的审计方法应用到 `device.rs::update_ss`（[device.rs:106-127](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L106-L127)）和 macs.rs 的 cookie 密钥，看配置变更与 cookie 轮换路径上是否同样贯彻「用完即抹」。
- **外部知识**：阅读 `zeroize` crate 文档与 RFC 6479/rustsec 关于「`#[inline(never)]` 防编译器消除清零」的讨论，理解「编译器为何可能把写零优化掉」以及 `clear_on_drop`/`zeroize` 如何用「易失写」（volatile write）对抗这种优化。
