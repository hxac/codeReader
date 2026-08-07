# 防回放窗口（RFC 6479）

## 1. 本讲目标

本讲精读 `src/wireguard/router/anti_replay.rs`——WireGuard 数据面里那个只有一百来行、却承担「抗重放」职责的滑动位图过滤器。读完后你应当能够：

- 说清为什么一个被 AEAD（ChaCha20-Poly1305）认证过的报文，仍然需要单独的防回放检查；
- 推导出滑动窗口大小 `WINDOW_SIZE = 1984` 是怎么由位图长度与字长算出来的，以及为什么它比位图总位数 `2048` 少了正好一个字（64 位）；
- 读懂 `check` / `update_store` / `update` 三个方法里把一个 `u64` 序号拆成「字槽 + 位号」的位运算，并解释环形缓冲的下标取模；
- 理解为什么 `update` 必须跑在路由器的**串行阶段**（`sequential_work`）而不是并行阶段，否则合法报文会因线程调度而被误丢。

本讲承接 u5-l3（接收管道），把其中「防回放为何放在串行阶段」这一句话展开成完整的算法与代码。

## 2. 前置知识

- **AEAD 与 nonce。** ChaCha20-Poly1305 这类 AEAD 算法要求：同一把密钥下，每个 `(nonce, 密钥)` 组合只能用一次。WireGuard 用一个单调递增的 `u64` 计数器充当 nonce 的低 8 字节（见 u5-l2）。AEAD 保证「密文未被篡改」和「解密出来的明文是真的」，但**它本身不保证你没见过这个计数器**。
- **重放攻击（replay attack）。** 攻击者把以前合法截获的密文原封不动再发一遍。AEAD 解密会成功（密文确实合法），于是旧报文被「重放」进隧道。防回放过滤器就是用来拒绝这种「已经处理过的计数器」的。
- **位图（bitmap）。** 用一个比特位表示「这个序号我见过没有」：0 = 没见过（接受），1 = 见过（拒绝）。一台 64 位机器上的一个 `u64` 就能装 64 个序号的状态。
- **RFC 6479。** IETF 给 IPsec AH/ESP 设计的滑动窗口防回放算法。WireGuard 的实现注释直接引用了它（[anti_replay.rs:3-4](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/anti_replay.rs#L3-L4)），并做了一处小改动（允许序号 0）。
- 建议先读过 u5-l3，知道 `ReceiveJob` 被拆成 `parallel_work`（解密 + 路由校验）与 `sequential_work`（防回放 + 写 TUN）两段。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/wireguard/router/anti_replay.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/anti_replay.rs) | 本讲主角。`AntiReplay` 结构体、全部常量、`check`/`update_store`/`update` 三方法，以及一段自测。 |
| [src/wireguard/router/receive.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/receive.rs) | `ReceiveJob`。并行阶段用注释点明「不能在此做防回放」，串行阶段才调用 `protector.lock().update(...)`。 |
| [src/wireguard/router/device.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/device.rs) | `DecryptionState` 结构，内含 `protector: Mutex<AntiReplay>` 字段，说明防回放状态是「每把接收密钥一份」。 |
| [src/wireguard/router/peer.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs) | `DecryptionState::new` 在密钥注入时 `AntiReplay::new()`，确认每个新会话密钥都从一张干净位图开始。 |
| [src/wireguard/constants.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/constants.rs) | `REJECT_AFTER_MESSAGES = u64::MAX - 16`，约束计数器上界，保证 `AntiReplay` 的 `last` 不会溢出回绕。 |

## 4. 核心概念与源码讲解

### 4.1 防回放的动机与 `AntiReplay` 数据结构

#### 4.1.1 概念说明

AEAD 解密成功只能说明「这串密文是那把密钥加密的、没被改过」，它**没有记忆**：把同一条合法密文发一万次，它会老老实实解密一万次。所以接收方必须自己维护一张「我已经收过哪些序号」的账本，对重复序号直接拒收。

WireGuard 的设计选择是：用一张固定大小（2048 位）的**滑动位图**当账本，而不是记录所有历史序号。原因是隧道是长连接、报文源源不断，序号空间是 `u64`（约 \(1.8\times10^{19}\)），不可能也无必要全部记住——只要记住「最近一段窗口」就够了，比窗口更老的序号一律拒绝，因为正常流量不会突然冒出一个很老的报文。

#### 4.1.2 核心流程

防回放过滤器的生命周期是：

1. 每注入一把新的接收会话密钥（`DecryptionState::new`），就 `AntiReplay::new()` 一张全零位图，`last = 0`。
2. 收到一个 AEAD 认证通过的报文后，取出它的计数器 `seq`（即传输头里的 `f_counter`）。
3. `update(seq)`：先 `check(seq)` 判断「是不是重放或太老」，通过则 `update_store(seq)` 把对应位置 1、必要时前移窗口。
4. `update` 返回 `false` → 丢包；返回 `true` → 继续写 TUN。

#### 4.1.3 源码精读

数据结构本身极简，一个位数组加一个「迄今见过的最大序号」：

```rust
pub struct AntiReplay {
    bitmap: [Word; BITMAP_LEN],
    last: u64,
}
```

- [`src/wireguard/router/anti_replay.rs:26-29`](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/anti_replay.rs#L26-L29)：`bitmap` 是一个长度为 `BITMAP_LEN`（64 位平台下 = 32）的 `Word` 数组，共 2048 位；`last` 是迄今见过的最大序号，初始 0。
- [`src/wireguard/router/anti_replay.rs:37-45`](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/anti_replay.rs#L37-L45)：`new()` 全零初始化，两条 `debug_assert` 在调试构建里核对常量自洽（\(2^{\text{SHIFTS}} = \text{SIZE\_OF\_WORD}\)、2048 能被字长整除）。

它被装在哪里？在每个接收密钥的解密状态里，一份独立的位图：

```rust
pub struct DecryptionState<E: Endpoint, C: Callbacks, T: tun::Writer, B: udp::Writer<E>> {
    pub(super) keypair: Arc<KeyPair>,
    pub(super) confirmed: AtomicBool,
    pub(super) protector: Mutex<AntiReplay>,   // ← 防回放过滤器
    pub(super) peer: Peer<E, C, T, B>,
}
```

- [`src/wireguard/router/device.rs:46-51`](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/device.rs#L46-L51)：注意是 `Mutex<AntiReplay>`，因为多个 worker 线程会并发触碰它（串行阶段的串行化靠的是保序队列 + 这把锁，见 4.4）。
- [`src/wireguard/router/peer.rs:133-142`](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L133-L142)：`DecryptionState::new` 里 `protector: spin::Mutex::new(AntiReplay::new())`，每把新密钥一张干净位图——这很重要，因为不同密钥的计数器各自从 0 开始，不能共用账本。

#### 4.1.4 代码实践

**实践目标：** 确认「每个会话密钥独享一张防回放位图」这一不变量。

**操作步骤：**

1. 打开 `src/wireguard/router/peer.rs`，找到 `DecryptionState::new`（约 133 行）。
2. 顺着调用方搜索：在 peer.rs 内查 `DecryptionState::new(` 的调用点（在 `add_keypair` / `confirm_key` 附近）。
3. 观察它是否在「每次新密钥进入 `recv` 表」时都被调用。

**需要观察的现象：** 每当一把新接收密钥被插入设备的 `recv` 表（`HashMap<receiver_id, DecryptionState>`），都会伴随一次 `DecryptionState::new`，从而得到一张全新的 `AntiReplay`。旧密钥退役时整个 `DecryptionState`（连同它的位图）被丢弃。

**预期结果：** 你会看到「新密钥 → 新位图」一一对应，不存在跨密钥复用位图的情况。这也解释了为什么 WireGuard 要定期重握手轮换密钥：每轮换一次，防回放状态自然清零。

#### 4.1.5 小练习与答案

**练习 1：** 既然 AEAD 已经认证了报文，为什么攻击者不能直接篡改报文里的 `f_counter` 字段，把它改成一个没见过的序号来绕过防回放？

**答案：** 因为 `f_counter` 既是防回放检查的输入，**也是 AEAD nonce 的一部分**（见 u5-l2：nonce = `[0u8;4] || counter`）。改了 `f_counter` 就等于换了 nonce，AEAD 的 Poly1305 标签校验会立刻失败，报文在 [`receive.rs:101-104`](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/receive.rs#L101-L104) 的 `open_in_place` 处被拒。所以「计数器不可伪造」是 AEAD 免费提供的，防回放只需管「记账」。

**练习 2：** 为什么把防回放状态挂在 `DecryptionState`（每密钥一份），而不是挂在 peer 上（每 peer 一份）？

**答案：** 同一个 peer 在密钥轮转期间会同时持有 current/previous/next 多把密钥（见 u5-l6 KeyWheel），每把密钥的计数器序列是独立的。若共用一张位图，不同密钥的相同序号会互相误判。每密钥一份位图才能正确区分。

---

### 4.2 字长自适应与窗口大小推导

#### 4.2.1 概念说明

这套算法把一个 `u64` 序号拆成「第几个字」和「字内第几位」两层坐标。一个字是多少位，取决于目标平台：64 位平台用 `u64`（64 位/字），32 位平台用 `u32`（32 位/字）。算法用一组 `#[cfg]` 常量在编译期适配，核心参数随字长而变。

理解本模块的关键是三个常量的来历：位图总位数 `BITMAP_BITLEN`、有效窗口 `WINDOW_SIZE`、以及它们之间「正好差一个字」的冗余关系。

#### 4.2.2 核心流程

序号到「位图坐标」的映射（以 64 位平台为例）：

\[ \text{seq} \;\longrightarrow\; \begin{cases} \text{bit\_location} = \text{seq} \,\&\, (2^{6}-1) & \text{低 6 位：字内位号（0\text{–}63）} \\[2pt] \text{word} = \text{seq} \gg 6 & \text{全局字号} \\[2pt] \text{index} = \text{word} \,\&\, (2^{5}-1) & \text{环形槽位（0\text{–}31）} \end{cases} \]

窗口大小推导：

\[ \text{BITMAP\_BITLEN} = 2048, \quad \text{SIZE\_OF\_WORD} = 64 \]
\[ \text{BITMAP\_LEN} = \frac{2048}{64} = 32 \text{（个字）} \]
\[ \text{WINDOW\_SIZE} = \text{BITMAP\_BITLEN} - \text{SIZE\_OF\_WORD} = 2048 - 64 = 1984 \]

为什么窗口是 `2048 - 64` 而不是 `2048`？因为位图被当成**环形缓冲**使用（`index` 对 32 取模）。设当前最大序号为 `last`，它在全局字号 `last >> 6`；仍在窗口内、最老的合法序号是 `last - WINDOW_SIZE = last - 1984`，它所在的字号是 `(last >> 6) - 31`（因为 \(1984 = 31 \times 64\)）。于是「在用字」横跨 32 个连续字号——正好占满环形缓冲的全部 32 个槽，`last` 的字与最老的字恰好**相邻而不重叠**（相差 31 个槽，模 32 即相邻）。若窗口取满 2048，最老字会与 `last` 的字落在同一个槽（模 32 相等），发生 aliasing。所以**冗余 1 个字（64 位）就是用来换取环形缓冲里 newest 字与 oldest 字永不撞号**。

> 一句话：位图有 2048 位，但只承诺对「最近 1984 个序号」精确记账，留 64 位做环形防撞的缓冲。

#### 4.2.3 源码精读

```rust
#[cfg(target_pointer_width = "64")]
type Word = u64;
#[cfg(target_pointer_width = "64")]
const REDUNDANT_BIT_SHIFTS: usize = 6;

#[cfg(target_pointer_width = "32")]
type Word = u32;
#[cfg(target_pointer_width = "32")]
const REDUNDANT_BIT_SHIFTS: usize = 5;
```

- [`src/wireguard/router/anti_replay.rs:6-16`](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/anti_replay.rs#L6-L16)：64 位平台 `Word = u64`、`REDUNDANT_BIT_SHIFTS = 6`（\(2^6 = 64\)）；32 位平台 `Word = u32`、`REDUNDANT_BIT_SHIFTS = 5`（\(2^5 = 32\)）。这个 shift 量就是「右移多少位得到字号」。

```rust
const SIZE_OF_WORD: usize = mem::size_of::<Word>() * 8;
const BITMAP_BITLEN: usize = 2048;
const BITMAP_LEN: usize = BITMAP_BITLEN / SIZE_OF_WORD;
const BITMAP_INDEX_MASK: u64 = BITMAP_LEN as u64 - 1;
const BITMAP_LOC_MASK: u64 = (SIZE_OF_WORD - 1) as u64;
const WINDOW_SIZE: u64 = (BITMAP_BITLEN - SIZE_OF_WORD) as u64;
```

- [`src/wireguard/router/anti_replay.rs:18-24`](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/anti_replay.rs#L18-L24)：六个常量一览。
  - `SIZE_OF_WORD`：字的位数（64 位平台 = 64）。
  - `BITMAP_LEN`：字数（64 位平台 = 32，32 位平台 = 64）。
  - `BITMAP_INDEX_MASK`：字号取模掩码（64 位平台 = `0x1F`，即 31）。
  - `BITMAP_LOC_MASK`：字内位号掩码（64 位平台 = `0x3F`，即 63）。
  - `WINDOW_SIZE`：有效窗口（恒为 1984，与字长无关——因为分子分母都含一个字长，抵消）。

#### 4.2.4 代码实践

**实践目标：** 手算 32 位平台下的常量值，验证窗口大小不变。

**操作步骤：**

1. 假想把 `target_pointer_width` 切到 `"32"`（不必真改源码，纸上推演即可）。
2. 依次计算 `SIZE_OF_WORD`、`BITMAP_LEN`、`BITMAP_INDEX_MASK`、`BITMAP_LOC_MASK`、`WINDOW_SIZE`。

**需要观察的现象 / 预期结果：**

| 常量 | 64 位平台 | 32 位平台 |
|------|-----------|-----------|
| `Word` | `u64` | `u32` |
| `REDUNDANT_BIT_SHIFTS` | 6 | 5 |
| `SIZE_OF_WORD` | 64 | 32 |
| `BITMAP_LEN`（字数） | 32 | 64 |
| `BITMAP_INDEX_MASK` | `0x1F` (31) | `0x3F` (63) |
| `BITMAP_LOC_MASK` | `0x3F` (63) | `0x1F` (31) |
| `WINDOW_SIZE` | 1984 | 1984 |

注意两点：`BITMAP_INDEX_MASK` 与 `BITMAP_LOC_MASK` 在两种平台下**正好互换**（因为字长与字数互为倒数关系：32×64 = 64×32 = 2048）；而 `WINDOW_SIZE` 恒为 1984，**与字长无关**。算法的字长适配就是这么干净。

#### 4.2.5 小练习与答案

**练习 1：** 为什么 `WINDOW_SIZE` 恰好与字长无关？

**答案：** \(\text{WINDOW\_SIZE} = \text{BITMAP\_BITLEN} - \text{SIZE\_OF\_WORD}\)。`BITMAP_BITLEN = 2048` 是固定的；`SIZE_OF_WORD` 随平台变（64 或 32），所以严格说 `WINDOW_SIZE` 在 32 位平台是 2016、在 64 位平台是 1984。修正上一句：它**并非完全无关**，而是「位图总长减一个字」。本表里写成 1984 是 64 位平台的值。这个练习的要点是理解公式本身——窗口永远比位图少一个字。

> 作者注：上表「1984」一行是 64 位平台的值。32 位平台下 `WINDOW_SIZE = 2048 - 32 = 2016`。本项目实际目标平台是 64 位 Linux，后续讨论默认 `WINDOW_SIZE = 1984`。

**练习 2：** 如果把 `BITMAP_BITLEN` 从 2048 改成 1024，`WINDOW_SIZE`（64 位平台）变成多少？冗余几个字？

**答案：** `WINDOW_SIZE = 1024 - 64 = 960`。`BITMAP_LEN = 16` 个字。仍冗余 1 个字（语义不变：newest 字与 oldest 字在 16 槽环形缓冲里相差 15 槽、相邻不撞）。冗余永远是「一个字」，与位图总长无关。

---

### 4.3 三方法精读：`check` / `update_store` / `update`

#### 4.3.1 概念说明

`AntiReplay` 对外只暴露一个方法 `update(seq)`，它内部组合了两个私有方法：`check`（只读查询）与 `update_store`（写位图 + 推窗口）。三者职责清晰：

- `check`：判断「这个序号能不能收」——是重放/太老则返回 `false`，否则 `true`。不改状态。
- `update_store`：假定 `check` 已通过，把序号记进位图，必要时把窗口前移、清掉过期字。
- `update`：先 `check` 再 `update_store`，是唯一对外入口。

#### 4.3.2 核心流程

`check(seq)` 的三段判定（伪代码）：

```
if seq > last:           return true     # 比见过最大的还大 → 全新，必非重放
if last - seq > WINDOW_SIZE: return false # 太老，跌出窗口 → 拒绝
bit  = seq & BITMAP_LOC_MASK              # 字内位号
idx  = (seq >> SHIFTS) & BITMAP_INDEX_MASK # 环形字槽
return bitmap[idx] & (1 << bit) == 0      # 该位为 0 → 没见过 → 接受
```

`update_store(seq)`（仅当 `seq > last` 时需推窗口）：

```
if seq > last:
    diff = (seq >> SHIFTS) - (last >> SHIFTS)   # 跨了几个字
    if diff >= BITMAP_LEN:  bitmap 全清零        # 跳太远，整张图作废
    else:                   把 last 字与 seq 字之间的每个字清零
    last = seq
idx = (seq >> SHIFTS) & BITMAP_INDEX_MASK
bitmap[idx] |= 1 << (seq & BITMAP_LOC_MASK)     # 置位
```

#### 4.3.3 源码精读

`check`：

```rust
// Unlike RFC 6479, zero is allowed.
fn check(&self, seq: u64) -> bool {
    if seq > self.last {
        return true;
    }
    if self.last - seq > WINDOW_SIZE {
        return false;
    }
    let bit_location = seq & BITMAP_LOC_MASK;
    let index = (seq >> REDUNDANT_BIT_SHIFTS) & BITMAP_INDEX_MASK;
    self.bitmap[index as usize] & (1 << bit_location) == 0
}
```

- [`src/wireguard/router/anti_replay.rs:47-64`](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/anti_replay.rs#L47-L64)。
- **第 49 行注释「Unlike RFC 6479, zero is allowed」**：RFC 6479 把序号 0 视作非法（IPsec 序号从 1 起），WireGuard 计数器从 0 起，故允许 0。初始 `last = 0` 时 `check(0)`：`0 > 0` 不成立；`0 - 0 = 0 > 1984` 不成立；bit 0 of word 0 = 0 → 接受。
- **下溢安全**：`self.last - seq` 看似可能下溢（`seq > last` 时 `u64` 减法回绕成巨大值），但第 52 行的 `if seq > self.last { return true }` 已经把这种情况挡住，保证走到第 56 行时必有 `seq <= last`，减法不会下溢。这是常见的「先比较再相减」防下溢写法。
- **环形映射**：`index = (seq >> 6) & 0x1F` 把全局字号折叠进 [0,31] 的环形槽；`bit_location = seq & 0x3F` 取字内位号。

`update_store`：

```rust
fn update_store(&mut self, seq: u64) {
    debug_assert!(self.check(seq));
    let index = seq >> REDUNDANT_BIT_SHIFTS;

    if seq > self.last {
        let index_cur = self.last >> REDUNDANT_BIT_SHIFTS;
        let diff = index - index_cur;

        if diff >= BITMAP_LEN as u64 {
            self.bitmap = [0; BITMAP_LEN];
        } else {
            for i in 0..diff {
                let real_index = (index_cur + i + 1) & BITMAP_INDEX_MASK;
                self.bitmap[real_index as usize] = 0;
            }
        }
        self.last = seq;
    }

    let index = index & BITMAP_INDEX_MASK;
    let bit_location = seq & BITMAP_LOC_MASK;
    self.bitmap[index as usize] |= 1 << bit_location;
}
```

- [`src/wireguard/router/anti_replay.rs:66-91`](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/anti_replay.rs#L66-L91)。
- `debug_assert!(self.check(seq))`：调试期断言「调用者已先 check」，是契约护栏。
- **推窗口（76-83 行）**：当新序号把 `last` 往前推时，要把「新 last 字」与「旧 last 字」之间被跨越的字清零——这些字代表的序号区间现在已落到窗口下沿，里面残留的旧位会误导后续 `check`。若一次跨越了整个位图（`diff >= BITMAP_LEN`），直接整张清零；否则逐字清。
- **注意 80 行的 `index_cur + i + 1`**：清的是 `index_cur+1 ..= index`，即旧字之后、新字（含）之前的所有字；旧字本身不清（它里面可能还有窗口内、`seq` 之下的合法未到位序号）。最后 88-90 行把新序号位置 1。
- 一个漂亮的副作用：当 `seq` 落在一个新字里，这个字先被清零、再只置 `seq` 那一位——高于 `seq` 的同字位（更小序号本应未见过）保持 0，语义正确。

`update`：

```rust
pub fn update(&mut self, seq: u64) -> bool {
    if self.check(seq) {
        self.update_store(seq);
        true
    } else {
        false
    }
}
```

- [`src/wireguard/router/anti_replay.rs:103-110`](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/anti_replay.rs#L103-L110)：对外唯一入口，返回 `true` = 接受并记账、`false` = 重放或太老应丢弃。

#### 4.3.4 代码实践

**实践目标：** 用源码注释把 `check` 的三个分支对应到「三种报文命运」。

**操作步骤：** 在 `check` 函数体里，为三段各加一行中文注释，标明它对应哪类报文。例如：

```rust
fn check(&self, seq: u64) -> bool {
    if seq > self.last {
        // 分支 A：全新最大序号 → 必然非重放，接受
        return true;
    }
    if self.last - seq > WINDOW_SIZE {
        // 分支 B：太老，已跌出 1984 窗口 → 拒绝
        return false;
    }
    let bit_location = seq & BITMAP_LOC_MASK;
    let index = (seq >> REDUNDANT_BIT_SHIFTS) & BITMAP_INDEX_MASK;
    // 分支 C：在窗口内，看位图里这一位有没有被置过
    self.bitmap[index as usize] & (1 << bit_location) == 0
}
```

**需要观察的现象：** 三段对应三类报文——(A) 全新最大序号、(B) 太老的序号、(C) 窗口内序号。只有 (C) 真正去查位图；(A)(B) 都靠序号比较短路。

**预期结果：** 你会发现「绝大多数正常流量命中分支 A」（连续递增），分支 C 主要在处理乱序/补发，分支 B 拒绝迟到的迟到报文。这是为热路径优化的：递增序号只需一次比较。

> 说明：本实践仅要求添加注释以辅助理解，不改变逻辑。如要真正修改源码，请先备份或在工作副本上操作，遵守「不修改源码」的总原则——本讲义只读。

#### 4.3.5 小练习与答案

**练习 1：** `check` 里 `index` 用了 `& BITMAP_INDEX_MASK`（取模），而 `update_store` 计算 `index_cur`/`diff` 时却没有立刻取模，为什么？

**答案：** `update_store` 需要计算「新字与旧字之间跨了几个字」`diff = index - index_cur`，这必须是**全局字号差**，所以用未取模的 `index = seq >> SHIFTS`（全 64 位）。取模后的环形槽位算不出正确的「距离」。只有在最后真正读写位图数组时（80、88 行），才用 `& BITMAP_INDEX_MASK` 把全局字号折回环形槽。`check` 不需要算距离，直接折回槽位即可。

**练习 2：** 假设 `last = 100`，位图已记录 0..=100。现在收到 `seq = 3000`（远超窗口）。`update_store` 走哪条分支？之后 `check(50)` 结果如何？

**答案：** `seq=3000 > last=100`，`index = 3000>>6 = 46`，`index_cur = 100>>6 = 1`，`diff = 45 >= BITMAP_LEN(32)` → 整张位图清零，`last = 3000`，置位 3000。之后 `check(50)`：`50 > 3000` 不成立；`3000 - 50 = 2950 > 1984` → 分支 B 返回 `false`（太老拒绝）。整张清零是合理的，因为跳了这么远，所有旧序号都已跌出窗口。

---

### 4.4 串行协作：为何 `update` 必须跑在 `sequential_work`

#### 4.4.1 概念说明

`AntiReplay` 看似是个纯算法模块，但它在哪里被调用、以什么顺序被调用，直接决定正确性。这是 u5-l3 埋下的伏笔：防回放检查被放在 `ReceiveJob::sequential_work`（保序串行阶段），而不是 `parallel_work`（并行解密阶段）。原因有两层：

1. **数据竞争**：`update` 取 `&mut self`，会改写 `bitmap` 和 `last`。多线程并发调用会数据竞争——这由 `Mutex<AntiReplay>` 的锁解决（互斥）。
2. **顺序敏感（更要命）**：即便加了锁保证互斥，「谁先拿到锁」在并行阶段是**由线程调度决定的，不可预测**。窗口的滑动（`last` 的前移）顺序若由调度决定，会让合法报文被误丢。这一层只能靠**保序队列**解决。

#### 4.4.2 核心流程

设想一个反面场景——假如 `update` 在并行阶段调用：

```
报文到达顺序（udp_worker 入队顺序）：seq=10, seq=11, ..., seq=2000
并行解密完成后，worker 抢着调用 update：
  线程 X 先 update(2000)  → last 跳到 2000
  线程 Y 后 update(10)    → last - 10 = 1990 > 1984 → 分支 B 判「太老」→ 丢弃！
```

报文 10 明明合法（它先到、也认证通过了），却因为「调度顺序让大序号先记账、把窗口一下推远」而被误判为太老。源码注释把这一点讲得很直白。

正确做法是让 `update` 在**保序队列的串行阶段**执行：报文按**到达顺序**（即入队顺序）依次进入 `sequential_work`，于是 `update` 的调用顺序 = 到达顺序。先到的先记账，窗口只随「真正先到的报文」滑动，绝不会越过尚未处理的已到达报文。

#### 4.4.3 源码精读

并行阶段那段著名注释：

```rust
/* The parallel section of an incoming job:
 *
 * - Decryption.
 * - Crypto-key routing lookup.
 *
 * Note: We truncate the message buffer to 0 bytes in case of authentication failure
 * or crypto-key routing failure (attempted impersonation).
 *
 * Note: We cannot do replay protection in the parallel job,
 * since this can cause dropping of packets (leaving the window) due to scheduling.
 */
```

- [`src/wireguard/router/receive.rs:55-65`](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/receive.rs#L55-L65)：作者明确写「We cannot do replay protection in the parallel job, since this can cause dropping of packets (leaving the window) due to scheduling.」——正是上面那个反面场景。

串行阶段才做防回放：

```rust
// check for replay
if !job.state.protector.lock().update(header.f_counter.get()) {
    log::debug!("inbound worker: replay detected");
    return;
}
```

- [`src/wireguard/router/receive.rs:157-161`](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/receive.rs#L157-L161)：在 `sequential_work` 里取 `protector` 锁、调 `update(f_counter)`。失败（重放/太老）则直接 `return` 丢包。
- 这里同时用到「保序队列」（见 u5-l4 的 `SequentialJob`，保证多个报文按入队顺序进串行阶段）与「`Mutex`」（保证对单张位图的互斥写）。二者缺一不可：保序队列管「顺序」，互斥锁管「数据竞争」。

把这和 u5-l3 的分工表对应：**并行阶段做无副作用的只读重活（解密、路由校验），串行阶段做对顺序敏感的副作用（防回放、确认密钥、写 TUN）**。防回放是这套分工最典型的例子。

#### 4.4.4 代码实践

**实践目标：** 基于 `anti_replay.rs` 自带的测试（[`tests` 模块](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/anti_replay.rs#L113-L157)），补一个用例，精确覆盖「窗口外拒绝、窗口内未见过接受、窗口内已见过拒绝」三种边界。

**操作步骤：** 在 `src/wireguard/router/anti_replay.rs` 末尾的 `mod tests` 内追加：

```rust
#[test]
fn anti_replay_window_boundary() {
    let mut ar = AntiReplay::new();

    // 连续接收 0..N
    let n: u64 = 5000;
    for i in 0..n {
        assert!(ar.update(i), "seq {} should be accepted", i);
    }
    assert_eq!(ar.last, n - 1);

    // (1) 窗口外（last - WINDOW_SIZE 之前）：跌出滑动窗口 → check 返回 false
    let too_old = ar.last - WINDOW_SIZE - 1; // = 4999 - 1984 - 1 = 3014
    assert!(!ar.check(too_old));
    assert!(!ar.check(0)); // 离窗口更远，同样拒绝

    // (2) 窗口内、但已出现过的序号（位图中该位已置 1）→ 重放，拒绝
    let inside_seen = n - 100; // = 4900，在 [last-W, last] 内且已 update
    assert!(ar.last - inside_seen <= WINDOW_SIZE); // 先确认它确实在窗口内
    assert!(!ar.check(inside_seen));

    // (3) 窗口内、从未出现过的序号（先制造一个空洞）→ 接受
    //     跳到 n+500，使 last = n+499；此时 (n..n+499) 区间内的序号从未 update
    let big = n + 500;
    assert!(ar.update(big));
    let hole = n + 10; // < big，落在窗口内，从未 update
    assert!(ar.last - hole <= WINDOW_SIZE); // 在窗口内
    assert!(ar.check(hole));                // 接受
    assert!(ar.update(hole));               // 记账
    assert!(!ar.check(hole));               // 同一序号再来 → 重放，拒绝

    // (4) 比 last 还大的全新序号 → 永远接受（分支 A）
    assert!(ar.check(ar.last + 1));
}
```

**需要观察的现象：** 用 `cargo test --lib anti_replay_window_boundary` 运行（项目默认 `cargo test` 也会跑到）。四组断言分别命中 `check` 的分支 B（太老）、分支 C（位置 1）、分支 C（位置 0）、分支 A（全新最大）。

**预期结果：** 测试通过。注意 `ar.last` 能在 `mod tests` 里直接读取，是因为 `tests` 是 `anti_replay` 的子模块（`use super::*`），可以访问私有字段。

> 说明：该测试是「示例代码」，需要你把它加入源码的 `mod tests` 才能运行；本讲义遵循「不修改源码」原则，仅提供可粘贴的用例供你在工作副本中验证。

#### 4.4.5 小练习与答案

**练习 1：** 既然 `protector` 已经是 `Mutex<AntiReplay>`，互斥有了，为什么还非得放进保序队列的串行阶段？锁本身不能保证顺序吗？

**答案：** 锁只保证「同一时刻只有一个线程在改」，**不保证「谁先改」**。在并行阶段，多个 worker 抢锁的顺序由 OS 调度决定，可能出现「序号大的报文先抢到锁、先 update，把 `last` 推远」的情况，导致序号小但先到达的报文被判太老而误丢。保序队列把 `update` 的调用顺序锁定为「报文到达顺序」，这才是消除「窗口被调度推远」的关键。互斥锁解决数据竞争，保序队列解决顺序敏感性，两者职责不同。

**练习 2：** 报文在并行阶段解密成功后、进入串行阶段前，`last` 完全没动过。这是否意味着两个报文可能同时通过 `check`、然后都进串行阶段、第二个被判重放？

**答案：** 是的，但这正是正确行为。`check` 在并行阶段**根本没被调用**（防回放整段在串行阶段）。两个报文都只是解密 + 路由校验通过，进入保序队列。串行阶段按顺序 `update`：第一个 `update(5)` 成功、置位；第二个 `update(5)` 再查时位已置 → 重放拒绝。这保证「同一序号只被处理一次」，且处理顺序确定。

---

## 5. 综合实践

把本讲的知识串成一条线索：**「序号 → 位图坐标 → 三分支判定 → 串行记账」**。

**任务：** 阅读一个真实的乱序到达场景，画出位图状态。

1. 设 64 位平台，`AntiReplay::new()`，`last = 0`。
2. 依次 `update` 以下序号：`5, 7, 6, 100, 8`。
3. 对每一步，记录：(a) 命中 `check` 的哪个分支；(b) `last` 的新值；(c) 哪些位被置 1。
4. 最后回答：`check(6)` 在第 3 步是否被接受？为什么？`check(100)` 在第 5 步（`last` 已是 100）的窗口内吗？

**参考解答：**

| 步骤 | seq | 分支 | last 后 | 行为 |
|------|-----|------|---------|------|
| 1 | 5 | A（5 > 0） | 5 | 置 word0 bit5 |
| 2 | 7 | A（7 > 5） | 7 | 置 word0 bit7 |
| 3 | 6 | C（6 ≤ 7，7−6=1 ≤ 1984，bit6=0） | 7 | 置 word0 bit6 → 接受 |
| 4 | 100 | A（100 > 7） | 100 | 推窗口：清 word0..word0 之后被跨的字（100>>6=1，diff=1，清 word1）；置 word1 bit(100&63=36) |
| 5 | 8 | C（8 ≤ 100，100−8=92 ≤ 1984，bit8=0） | 100 | 置 word0 bit8 → 接受 |

- 第 3 步 `update(6)` **被接受**：虽然 6 < 当前 `last=7`，但它在窗口内、位未置，是合法乱序补发，不是重放。这正是滑动位图相对「严格递增检查」的优势——容忍乱序。
- 第 5 步时 `last=100`，序号 100 落在 `word1`，`check(100)`：`100 > 100` 不成立；`100-100=0 ≤ 1984`，进分支 C，bit36 of word1 已置 1 → 返回 `false`（重放）。100 在「窗口上沿」（就是 `last` 本身）。

**延伸思考：** 若把上面 5 步改成全部并行、调度顺序为 `100, 5, 7, 6, 8`，哪些会被误丢？（答：先 `update(100)` 让 `last=100`，随后 `5/6/7/8` 都在窗口内（100−8=92 < 1984），所以**这种规模下不会被误丢**；只有当跨度超过 `WINDOW_SIZE=1984` 时，调度乱序才会真正造成误丢——见 4.4.2 的极端例子。这也解释了为什么作者选 2048 位这么大的窗口：给调度乱序留足缓冲。）

## 6. 本讲小结

- `AntiReplay` 是一个 2048 位滑动位图 + 一个 `last`（最大已见序号），每个接收会话密钥独享一份（`DecryptionState::new` 时全新初始化）。
- 一个 `u64` 序号被拆成两层坐标：低 6 位是「字内位号」（`BITMAP_LOC_MASK`），高位右移 6 再模 32 是「环形字槽」（`BITMAP_INDEX_MASK`）；字长由 `#[cfg]` 在 32/64 位平台间自适应。
- `WINDOW_SIZE = BITMAP_BITLEN - SIZE_OF_WORD`（64 位平台 = 1984），冗余的一个字用来防止环形缓冲里 newest 字与 oldest 字撞号。
- `check` 三分支：`seq > last` 全新接受、`last - seq > WINDOW_SIZE` 太老拒绝、否则查位图（位 0 接受、位 1 重放）。`update = check + update_store` 是唯一对外入口。
- `update_store` 在 `seq > last` 时把跨越的字清零（超过整图则全清），再置位；旧字保留以容纳窗口内的乱序补发。
- **`update` 必须在保序队列的串行阶段调用**：并行阶段若做防回放，线程调度会把窗口推远、误丢合法报文（[`receive.rs:63-65`](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/receive.rs#L63-L65) 有明确注释）。互斥锁管数据竞争，保序队列管顺序敏感性。

## 7. 下一步学习建议

- **回到 u5-l6**：把 `AntiReplay` 放回 KeyWheel 语境——current/previous/next 三把密钥各自带一张位图，密钥轮转时旧位图随 `DecryptionState` 一起退役，新位图从零开始，体会「重握手 = 防回放重置」。
- **对照 u5-l4**：重读保序队列 `Queue` 的 `contenders` 原子接力机制，理解它如何保证「报文按入队顺序进 `sequential_work`」，这正是本讲 `update` 能正确串行化的底层支撑。
- **阅读 RFC 6479 原文**（代码注释里的链接 https://tools.ietf.org/html/rfc6479 ），对比 WireGuard 实现「允许序号 0」「无失败重传握手」等与 IPsec 语境的差异。
- **若想继续往安全方向深入**：结合 u7-l2（密钥材料清零），思考 `AntiReplay` 本身虽不含密钥，但 `DecryptionState` 整体被 `Arc` 共享、退役时的回收路径如何保证不残留可被利用的状态。
