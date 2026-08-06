# 密钥轮转 KeyWheel 与 Peer 生命周期

## 1. 本讲目标

本讲聚焦路由器数据面的「会话密钥」管理：握手模块（u4-l3）协商出来的 `KeyPair` 是怎么进入路由器、怎么被使用、又怎么被轮换和清理的。

学完后你应该能够：

- 说清 `KeyWheel` 里 `next` / `current` / `previous` 三个槽位的语义，以及它们何时发生轮转。
- 解释为什么「发送」只需要一个加密状态、而「接收」却要维护多个解密状态。
- 看懂 `add_keypair` 在 initiator（发起方）与非 initiator（响应方）两条分叉上的不同处理与确认机制。
- 掌握「无密钥可用」时如何用 `staged_packets` 暂存数据包并触发 `need_key` 回调发起握手。
- 理解 `PeerHandle` 被 drop 时如何把 peer 从设备里干净地拆除（清路由表、释放 receiver id、清零密钥）。

## 2. 前置知识

阅读本讲前，建议你已经建立以下认知（来自前置讲义，本讲不再重复）：

- **握手/路由器分离**（u1-l1、u3-l1）：握手模块用 Noise IK 协商出对称会话密钥 `KeyPair`，交给路由器对传输报文做对称加解密。路由器自己不算密码学，密钥是「注入」进来的。
- **KeyPair 的结构**（u4-l3）：一个 `KeyPair` 含 `send`/`recv` 两个 `Key`（各 32 字节密钥 + 4 字节 id）、一个 `initiator: bool` 标志、以及出生时间 `birth`。
- **两阶段任务模型**（u5-l1、u5-l4）：路由器把每个报文拆成「可并行的 `parallel_work`」和「保序的 `sequential_work`」两段，靠 per-peer 的 `Queue` 实现并行加密、有序发送。
- **receiver id**（u4-l1、u4-l2）：传输报文头里的 4 字节 `f_receiver`，是握手模块分配的短期标识，接收方靠它在 `recv` 表里查到对应的解密状态。

一个贯穿全讲的关键直觉：**WireGuard 的密钥是有寿命的**。单个会话密钥加密的报文数和存活时间都有上限（`REJECT_AFTER_MESSAGES`、`REJECT_AFTER_TIME`），到点前要主动重握手换密钥（`REKEY_AFTER_*`）。换密钥不能「啪一下」切断旧密钥——网络里还飞着用旧密钥加密的报文，所以新旧密钥必须能短暂共存。`KeyWheel` 就是管理这种「新旧共存」状态机。

## 3. 本讲源码地图

本讲主要精读两个文件：

| 文件 | 作用 |
|------|------|
| [src/wireguard/router/peer.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs) | Peer 的全部状态与行为：`KeyWheel` 定义、`Peer`/`PeerHandle` 句柄、`add_keypair`/`confirm_key`/`send`/`send_staged`、Drop 清理。本讲主角。 |
| [src/wireguard/router/device.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/device.rs) | 设备级状态：`EncryptionState`/`DecryptionState` 定义、`recv` 表（receiver id → 解密状态）、`table`（cryptokey 路由表）、`Device::send`/`recv` 入口。 |

辅助理解（引用但非精读对象）：

| 文件 | 作用 |
|------|------|
| [src/wireguard/types.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/types.rs) | `Key`/`KeyPair` 定义、`Key::Drop` 清零、`KeyPair::local_id`。 |
| [src/wireguard/router/receive.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/receive.rs) | 接收管道里触发 `confirm_key` 的那一行——密钥静默确认的起点。 |
| [src/wireguard/workers.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs) | 握手工作线程调用 `peer.add_keypair(kp)` 并把返回的 id 交还握手层 `device.release`。 |

## 4. 核心概念与源码讲解

### 4.1 Peer 的三层句柄：PeerInner / Peer / PeerHandle

#### 4.1.1 概念说明

路由器里「一个 peer」对应一整套状态：它的密钥轮转状态、加密状态、暂存包、端点地址、两条保序队列等等。这些状态很重，且需要在多个工作线程之间共享。项目用三层类型来表达「同一个 peer 的不同持有方式」：

- `PeerInner`：peer 的**真实状态**，被 `Arc` 包着。
- `Peer`：一个可 `Clone` 的**共享引用**（内部就是 `Arc<PeerInner>`），可以廉价复制进各工作线程。多个 `Peer` 是否指向同一个 peer 用 `Arc::ptr_eq` 判等。
- `PeerHandle`：一个**不可克隆的专属引用**，drop 时会把 peer 从设备里删除。它是 peer 的「所有权句柄」，由设备持有；其余代码拿到的是 `Peer`。

#### 4.1.2 核心流程

```
Device::new_peer(opaque)
        │  调用 new_peer(device, opaque)
        ▼
   PeerHandle { peer: Peer { inner: Arc<PeerInner{...}> } }
        │  .clone() 出 Peer 散布到工作线程
        ▼
   多个 Peer (Arc 引用计数增加)
        │  Device 持有的 PeerHandle 被 drop
        ▼
   Drop for PeerHandle：从设备删除 peer、释放 id、清零密钥
```

`PeerHandle` 不能 `Clone`（注意它**没有**实现 `Clone`，而 `Peer` 实现了），这从类型层面保证「删除 peer」这件事只有一个发起者。

#### 4.1.3 源码精读

`PeerInner` 的字段就是「一个 peer 的全部家当」：

[src/wireguard/router/peer.rs:38-47](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L38-L47) — 定义 `PeerInner`，字段含义：`device`（回指设备，用于访问 recv 表/路由表/工作队列）、`opaque`（上层回调用不透明类型，如定时器状态）、`outbound`/`inbound`（两条保序队列）、`staged_packets`（无密钥时暂存的数据包）、`keys`（KeyWheel）、`enc_key`（当前加密状态）、`endpoint`（对端地址）。

`Peer` 与 `PeerHandle` 的定义非常薄：

[src/wireguard/router/peer.rs:64-75](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L64-L75) — `Peer` 仅包一个 `Arc<PeerInner>`；`PeerHandle` 仅包一个 `Peer`。

二者都 `Deref` 到 `PeerInner`，所以拿到 `Peer` 或 `PeerHandle` 就能像用 `PeerInner` 一样直接访问其字段/方法：

[src/wireguard/router/peer.rs:100-114](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L100-L114) — `Peer` 与 `PeerHandle` 的 `Deref` 实现，目标都是 `PeerInner`。

> 补充：`PeerInner` 还额外 `Deref` 到 `C::Opaque`（[peer.rs:55-61](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L55-L61)）。这样路由器代码既能「拿走」opaque 的所有权去做回调，外部又能透过 `Peer` 指针访问 opaque 暴露的其它功能（注释里举的例子是定时器状态）。本讲用到的不多，了解即可。

`new_peer` 是构造入口，所有字段初始化为空（无密钥、无端点）：

[src/wireguard/router/peer.rs:187-213](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L187-L213) — `new_peer` 把 `KeyWheel` 三槽全部置 `None`、`enc_key`/`endpoint` 置 `None`、两条队列用 `Queue::new()` 创建，返回 `PeerHandle`。

#### 4.1.4 代码实践

**实践目标**：建立对三层句柄所有权关系的直觉。

**操作步骤**：

1. 打开 [peer.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs)，搜索 `impl.*Clone for Peer` 与 `impl.*Drop for PeerHandle`。
2. 确认 `PeerHandle` **没有** `impl Clone`（在全文件搜 `PeerHandle` 出现的 `impl` 块）。

**需要观察的现象**：`Peer` 有 `Clone`（[peer.rs:77-83](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L77-L83)），`PeerHandle` 有 `Drop`（[peer.rs:144-185](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L144-L185)）但无 `Clone`。

**预期结果**：你能用一句话说清——「`Peer` 是可复制的共享引用，`PeerHandle` 是不可复制的所有权句柄，drop 它就等于删除 peer」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Peer` 的相等性用 `Arc::ptr_eq` 而不是逐字段比较？
**答案**：两个 `Peer` 是否「同一个 peer」取决于它们是否指向同一份 `PeerInner` 状态（指针相等），而不是字段值是否相同。逐字段比较既昂贵又会引发锁竞争，且语义错误——两个不同 peer 完全可能碰巧有相同的字段快照。

**练习 2**：`Device::new_peer` 返回的是 `PeerHandle` 还是 `Peer`？为什么？
**答案**：返回 `PeerHandle`（见 [device.rs:172-174](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/device.rs#L172-L174)）。因为调用方（配置层）需要持有 peer 的所有权，以便在删除 peer 时触发 `Drop` 把它从设备里拆除；工作线程需要的只是可复制的 `Peer`，由 `PeerHandle` deref/克隆派生。

---

### 4.2 KeyWheel：next / current / previous 三密钥轮转

#### 4.2.1 概念说明

`KeyWheel`（密钥轮）是本讲的核心数据结构。它用三个 `Option<Arc<KeyPair>>` 槽位表达一个 peer 当前持有的会话密钥集合：

| 槽位 | 语义 | 谁能用来加密 | 谁能用来解密 |
|------|------|------------|------------|
| `current` | 当前正在使用的密钥 | ✅ 是（`enc_key` 由它派生） | ✅ 是 |
| `previous` | 上一代密钥，即将退役 | ❌ 否 | ✅ 是（网络里还有旧密文） |
| `next` | 刚握手出来、**尚未确认**的密钥 | ❌ 否（等确认后转正） | ✅ 是（已插入 recv 表） |

为什么需要三个槽？因为密钥轮换是一个**有时间重叠的过程**：

- 新密钥握手成功后，不能立刻丢弃旧密钥——链路上还有用旧密钥加密的报文正在飞，接收方必须还能用 `previous` 解密它们。
- 响应方收到的新密钥在收到对方第一个数据报文前是「未确认」的，先放进 `next` 憋着，确认后再升格为 `current`。

`retired` 是一个 `Vec<u32>`，记录「已经被新一轮密钥挤出、等待交还给握手层释放的 receiver id」。

#### 4.2.2 核心流程

`KeyWheel` 的轮转由两个事件驱动，二者对应「发起方」和「响应方」两种角色（详见 4.4）：

```
【initiator（发起方）握手成功，新密钥已确认】
  新密钥 → current
  旧 current → previous       （previous 仍可解密旧报文）
  旧 previous → 丢弃（id 记入 retired，待 release）

【非 initiator（响应方）收到新密钥，尚未确认】
  新密钥 → next               （还不能用来加密，等首个报文确认）
  旧 next → previous          （旧的未确认密钥作废，id 记入 retired）

【响应方收到首个数据报文，确认 next（见 4.5 confirm_key）】
  next → current              （转正，开始用于加密）
  旧 current → previous
  旧 previous → 丢弃
```

注意两条路径的对称之美：每次「晋升」都把高槽位往下推一格（current→previous 或 next→previous），空出的高位放入新密钥。

#### 4.2.3 源码精读

`KeyWheel` 的定义极其简洁：

[src/wireguard/router/peer.rs:31-36](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L31-L36) — 三个 `Option<Arc<KeyPair>>` 槽位加一个 `retired: Vec<u32>`。注释点明：`next` 未确认、`current` 用于加密、`previous` 用于解密旧报文。

> 为什么 `KeyWheel` 不自己存「能否解密」的信息？因为「能否解密」由 recv 表（`receiver id → DecryptionState`）决定，与 KeyWheel 解耦。KeyWheel 只管「加密用谁」和「哪些 id 该退役」。两者在 `add_keypair` 里协同更新（见 4.4）。

`KeyWheel` 由 `Mutex` 保护（`keys: Mutex<KeyWheel>`，见 [peer.rs:44](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L44)），所有轮转操作都在持锁临界区内完成，保证状态机原子推进。

`KeyPair` 与 `local_id` 的定义在 types 层：

[src/wireguard/types.rs:31-56](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/types.rs#L31-L56) — `KeyPair` 含 `birth`/`initiator`/`send`/`recv`；`local_id()` 返回 `self.recv.id`，即本端接收用的 receiver id（用它去 recv 表查解密状态）。

> 注意字段命名的一个小陷阱：`KeyPair::initiator` 的文档注释写的是「has the key-pair been confirmed?」（[types.rs:34](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/types.rs#L34)）。也就是说 `initiator == true` 同时意味着「发起方」和「已确认」——因为发起方握手成功后会立刻发数据，密钥天然算作已确认（详见 4.4、4.5）。这个「一字段两含义」是理解 `add_keypair` 分叉的关键。

#### 4.2.4 代码实践

**实践目标**：把「三槽位 + 两个驱动事件」固化成一张可视的状态转移图。

**操作步骤**：

1. 在纸上画三个并排方框，分别标 `previous`、`current`、`next`。
2. 用两种颜色的箭头分别画「initiator 握手成功」和「非 initiator 收到新密钥 + 首报文确认」两条路径，标注每个槽位的新旧值去向。
3. 在 `previous` 方框旁标「仍可解密飞在路上的旧报文」，在 `next` 方框旁标「待 confirm_key 转正」。

**需要观察的现象**：两条路径都遵循「高位 → 低位」的单向流动，没有任何一个新密钥跳过 `current`/`next` 直接进 `previous`。

**预期结果**：你能指着图说清——「`current` 永远是加密用的密钥，`previous` 是上一代仍在解密的密钥，`next` 是响应方等待确认的密钥」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `previous` 槽位只用于解密、不再用于加密？
**答案**：因为 `previous` 是已经被 `current` 取代的旧密钥。旧密钥的 nonce 计数早已不再推进，且 WireGuard 规定密钥使用到一定报文数/时间后必须淘汰（`REJECT_AFTER_MESSAGES`）。再用它加密既会重复 nonce（破坏 AEAD 安全性），也违背密钥轮换初衷。但链路上可能还有用它加密的报文未送达，所以保留解密能力直到下一轮密钥到来。

**练习 2**：`retired: Vec<u32>` 里存的是什么？谁来消费它？
**答案**：存的是「已被挤出 KeyWheel、不再使用的 receiver id」。`add_keypair` 在返回前会把 `retired` 连同本次新释放的 id 一起作为 `Vec<u32>` 返回（[peer.rs:436-498](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L436-L498)），握手工作线程拿到后逐个调用 `device.release(id)`（[workers.rs:241-243](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L241-L243)），把 id 从握手模块的 `id_map` 里删掉，完成「借出必归还」。

---

### 4.3 加密态与解密态的不对称：EncryptionState 与 DecryptionState

#### 4.3.1 概念说明

接收 4.2 后你可能有个疑问：KeyWheel 里 current/previous/next 三个密钥都能解密，那「解密状态」存哪？而加密只用 current 一个，加密状态又存哪？答案是**两种状态用两套完全不同的存储方式**，这正反映了收发方向的不对称：

- **发送方向**：同一时刻只用一个密钥（current）加密，需要一个**单调递增的 nonce 计数器**。所以每个 peer 只有一个 `EncryptionState`，存在 peer 自己的 `enc_key: Mutex<Option<EncryptionState>>` 字段里。
- **接收方向**：同一时刻可能要用 current **或** previous 解密（取决于对端用哪个发的），靠报文头里的 `receiver id` 选密钥。所以解密状态是**多个**，存在设备级的 `recv: HashMap<u32, Arc<DecryptionState>>` 表里，按 receiver id 索引。

这种「一对一 vs 一对多」的不对称，是路由器数据面设计的一个核心洞察。

#### 4.3.2 核心流程

```
发送：
  peer.enc_key (Option<EncryptionState>)
     └─ keypair: Arc<KeyPair>   ← 当前 current 密钥
        nonce: u64              ← 每发一个包 +1，作 AEAD nonce

接收：
  device.recv (HashMap<u32, Arc<DecryptionState>>)
     ├─ receiver_id_A → DecryptionState { keypair=current, confirmed, AntiReplay, peer }
     └─ receiver_id_B → DecryptionState { keypair=previous, ... }   ← 仍可解旧报文
```

为什么发送不需要「防回放」、接收不需要「nonce 计数」？因为：

- 发送方自己控制 nonce 递增，天然单调，不会回退——只需保证「不溢出/不重复」（见 4.6 的 `REJECT_AFTER_MESSAGES` 检查）。
- 接收方要防的是**对端**重放或乱序发包，所以需要一个滑动位图（`AntiReplay`，见 u5-l7）来判定每个 counter 是否新鲜。

#### 4.3.3 源码精读

两个状态的定义都在 device.rs：

[src/wireguard/router/device.rs:41-51](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/device.rs#L41-L51) — `EncryptionState` 只有两个字段：`keypair` 和 `nonce: u64`；`DecryptionState` 有四个：`keypair`、`confirmed: AtomicBool`、`protector: Mutex<AntiReplay>`、`peer`。

设备的 recv 表定义：

[src/wireguard/router/device.rs:34](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/device.rs#L34) — `recv: RwLock<HashMap<u32, Arc<DecryptionState<...>>>>`，注释写明「receiver id -> decryption state」。`Device::recv` 入口正是靠报文头的 `f_receiver` 在这张表里查 `DecryptionState`（[device.rs:236-239](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/device.rs#L236-L239)）。

两者的构造函数（`EncryptionState::new` / `DecryptionState::new`）放在 peer.rs：

[src/wireguard/router/peer.rs:124-142](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L124-L142) — `EncryptionState::new` 把 nonce 归零、克隆 keypair；`DecryptionState::new` 把 `confirmed` 初值设为 `keypair.initiator`（即 initiator 密钥一出生就视为已确认，非 initiator 需等首报文），并新建一个 `AntiReplay`。

> 注意 `DecryptionState` 持有一个 `peer: Peer<...>`（可克隆的共享引用），这样解密 job 在并行/串行阶段都能回调到 peer（比如 `confirm_key`、写 TUN、更新端点）。而 `EncryptionState` 不持有 peer——因为加密 job 由 `Peer::send` 创建时已经把 `self.clone()` 传给了 `SendJob`（见 4.6）。

#### 4.3.4 代码实践

**实践目标**：验证「发送一个加密状态、接收多个解密状态」的存储差异。

**操作步骤**：

1. 在 [peer.rs:38-47](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L38-L47) 确认 `enc_key` 是 `Mutex<Option<EncryptionState>>`（单值）。
2. 在 [device.rs:25-39](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/device.rs#L25-L39) 确认 `recv` 是 `HashMap<u32, Arc<DecryptionState>>`（多值，设备级）。

**需要观察的现象**：`enc_key` 隶属于单个 peer；`recv` 隶属于设备、跨所有 peer 共享一张表，靠 receiver id 区分。

**预期结果**：你能解释「为什么查解密状态要去设备级 recv 表、而查加密状态直接问 peer 自己」——因为发送是 peer 私有的单密钥行为，接收是设备需要对所有 peer 的所有活跃密钥统一寻址。

#### 4.3.5 小练习与答案

**练习 1**：`DecryptionState.confirmed` 的初值由什么决定？为什么？
**答案**：由 `keypair.initiator` 决定（[peer.rs:136](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L136)）。initiator 密钥一出生就视为已确认（`true`），因为发起方握完手会立即发数据；非 initiator 密钥初值 `false`，要等收到对方首个报文才在接收管道里 `swap(true)` 触发一次 `confirm_key`（见 4.5）。

**练习 2**：为什么 `EncryptionState` 不需要 `AntiReplay`？
**答案**：防回放是接收侧的概念——防止对端把已收过的报文重发。发送侧自己递增 nonce，不存在「自己对自己重放」的问题，所以 `EncryptionState` 只需一个 nonce 计数器即可。

---

### 4.4 注入新密钥：add_keypair（initiator 与非 initiator 的分叉）

#### 4.4.1 概念说明

握手成功后，握手模块产出一个 `KeyPair`，经握手工作线程交给路由器。路由器侧的入口是 `PeerHandle::add_keypair`。这是 KeyWheel 状态机最关键的一次「推进」，它在 initiator 与非 initiator 两条路径上行为截然不同：

- **initiator == true**（我是发起方，刚收到对端的 Response）：新密钥已确认，**立即用于加密**。把它放进 `current`，旧 `current` 降到 `previous`，并立刻设置 `enc_key`。同时主动发一个包（暂存包或 keepalive）来「确认」这把密钥。
- **initiator == false**（我是响应方，刚发出 Response）：新密钥**尚未确认**，先放进 `next` 憋着，**不设置 enc_key**。要等对方发来第一个数据报文（触发 `confirm_key`）才转正。

#### 4.4.2 核心流程

```
add_keypair(new):
  锁 keys
    取出 retired（清空待返回）
    if new.initiator:                         # 发起方
        enc_key = EncryptionState::new(new)   # 立即可加密
        previous = current                    # current 降级
        current  = new
    else:                                     # 响应方
        previous = next                       # 旧 next 作废
        next    = new                         # 新密钥入 next 待确认
    # 更新 recv 表：
    recv.remove(previous.local_id())          # 退役旧槽的解密状态
    recv.insert(new.recv.id, DecryptionState::new(new))   # 注册新密钥解密状态
  解锁 keys
  if initiator:
      尝试 send_staged()，没有暂存包就 send_keepalive()   # 主动确认
  return retired + 本次释放的 id              # 交还握手层 release
```

注意两个不变量：

1. **KeyWheel 最多 3 把密钥**——previous/current/next 各一把。函数末尾有 `debug_assert!(release.len() <= 3)` 守护（[peer.rs:493-496](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L493-L496)）。
2. **recv 表与 KeyWheel 协同**——每次退役一个槽位，对应从 recv 表删一个 DecryptionState；每次新增密钥，对应往 recv 表插一个 DecryptionState。

#### 4.4.3 源码精读

`add_keypair` 的两条分叉：

[src/wireguard/router/peer.rs:436-478](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L436-L478) — initiator 分支设 `enc_key` 并把 `current` 降到 `previous`、新密钥入 `current`；非 initiator 分支把旧 `next` 降到 `previous`、新密钥入 `next`（不设 `enc_key`）。随后统一更新 recv 表：删 `previous` 的解密状态、插入新密钥的 `DecryptionState`。

注释明确写出设计意图：

[src/wireguard/router/peer.rs:420-435](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L420-L435) — 文档说明：返回值是待释放的 id 列表，**最多 3 个**，因为 KeyWheel 最多 3 把密钥，而新增密钥的唯一途径就是本方法。

initiator 分支在解锁后的「主动确认」：

[src/wireguard/router/peer.rs:480-491](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L480-L491) — initiator 必须主动发一个包来确认密钥：先试 `send_staged()` 补发暂存包，若没有暂存包就 `send_keepalive()` 发一个空保活包。注释写「is initiator, must confirm the key」。

> 为什么发起方要主动确认？这呼应 4.2 里 `initiator` 字段「已确认」的语义。发起方握完手立刻有加密能力（`enc_key` 已设），但 WireGuard 协议要求发起方发一个包让响应方借此确认密钥有效——否则响应方的 `next` 永远憋着转不了正。所以 `add_keypair` 在 initiator 分支主动触发一次发送。

调用方（握手工作线程）拿到返回的 id 后交还握手层：

[src/wireguard/workers.rs:241-243](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L241-L243) — `for id in peer.add_keypair(kp) { device.release(id); }`，把路由器退役的 receiver id 还给握手模块的 `id_map` 删除。

#### 4.4.4 代码实践

**实践目标**：用 dummy keypair 验证两条分叉对 KeyWheel 的不同影响。

**操作步骤**：

1. 打开测试 [src/wireguard/router/tests/tests.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/tests/tests.rs)，找到 `dummy_keypair(true)` 与 `dummy_keypair(false)` 的调用点（约 [tests.rs:184](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/tests/tests.rs#L184)、[tests.rs:328](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/tests/tests.rs#L328)、[tests.rs:372](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/tests/tests.rs#L372)）。
2. 观察 `peer1.add_keypair(dummy_keypair(false))` 与 `peer2.add_keypair(dummy_keypair(true))` 是如何配对使用的——这正是「响应方 false + 发起方 true」的一对密钥注入。

**需要观察的现象**：两个 peer 用相反的 `initiator` 值注入密钥后，只有 `dummy_keypair(true)` 的那方会立即触发发送（确认），false 那方则静默等待。

**预期结果**：你能对应到 4.4.2 的流程图，说清「true 进 current 并主动发包、false 进 next 等确认」。

**待本地验证**：若要实际跑测试，执行 `cargo test --package wireguard-rs test_` 相关用例（具体用例名以本地 `cargo test -- --list` 输出为准）。

#### 4.4.5 小练习与答案

**练习 1**：非 initiator 分支里，`keys.previous = keys.next.as_ref().cloned()` 这一步处理的是什么情况？
**答案**：处理「上一轮握手产出的 next 还没来得及确认，新一轮握手又来了」的情况。旧的未确认 `next` 被降级到 `previous`（随后其 recv id 被删除并加入 release），给新 `next` 腾位置。这保证 KeyWheel 的 `next` 槽永远只放最新一把待确认密钥。

**练习 2**：`add_keypair` 为什么在持 `keys` 锁的同一临界区里还要操作 recv 表？
**答案**：为了保持 KeyWheel 与 recv 表的一致性。如果分开加锁，可能出现「KeyWheel 里 current 已更新、但 recv 表还指着旧 DecryptionState」的中间态，此时若并发收到报文会查到错误的解密状态。在同一把 `keys` 锁内同步更新两者（recv 表用写锁），让状态推进对外是原子的。

---

### 4.5 静默确认：confirm_key 与首个报文转正

#### 4.5.1 概念说明

4.4 里非 initiator（响应方）的新密钥进了 `next`，还不能加密。它什么时候转正？答案是：**收到对方用这把密钥发的第一个数据报文时**。这叫「静默确认」（implicit confirmation）——WireGuard 不为确认单独发报文，而是把「能正确解密对方的首个传输报文」当作密钥有效的证明。

触发点在接收管道（u5-l3）：当某 `DecryptionState` 第一次被成功使用，它的 `confirmed` 标志从 `false` 翻成 `true`，顺势调用 `peer.confirm_key`。

#### 4.5.2 核心流程

```
接收管道 sequential_work（receive.rs）：
  if !state.confirmed.swap(true):     # 首次确认（原子交换）
      peer.confirm_key(&state.keypair)

confirm_key(keypair):
  锁 keys
    若 keys.next 不等于 keypair → 直接返回（不是待确认的那把）
    ekey = EncryptionState::new(next)
    轮转：next→current, current→previous, 旧 previous 丢弃
    C::key_confirmed(opaque)          # 通知上层（如停掉重握手定时器）
    enc_key = ekey                    # 至此响应方才有加密能力
  解锁
  send_staged()                       # 把握手期间攒下的包补发出去
```

注意 `confirm_key` 只在 `keys.next == keypair` 时才推进——防止把一把已经过时/被替换的密钥误转正。

#### 4.5.3 源码精读

触发点在接收管道：

[src/wireguard/router/receive.rs:163-167](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/receive.rs#L163-L167) — `if !job.state.confirmed.swap(true, Ordering::SeqCst) { peer.confirm_key(&job.state.keypair); }`。`swap` 返回旧值，只有首次（旧值 false）才进入分支，保证一个 `DecryptionState` 只触发一次确认。

`confirm_key` 的核心是一段精巧的「三步 swap」轮转：

[src/wireguard/router/peer.rs:316-349](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L316-L349) — 先校验 `next == keypair`，新建 `EncryptionState`，再用三次 `mem::swap` 把 `next` 提升为 `current`。

把三次 swap 的效果逐步展开（设 `swap` 临时变量初值为 `None`）：

| 步骤 | 语句 | `keys.next` | `swap` | `keys.current` | `keys.previous` |
|------|------|-------------|--------|----------------|-----------------|
| 0 | `let mut swap = None;` | 旧 next | None | 旧 current | 旧 previous |
| 1 | `swap(&mut next, &mut swap)` | **None** | 旧 next | 旧 current | 旧 previous |
| 2 | `swap(&mut current, &mut swap)` | None | 旧 current | **旧 next** | 旧 previous |
| 3 | `swap(&mut previous, &mut swap)` | None | 旧 previous | 旧 next | **旧 current** |

最终：`next = None`、`current = 旧 next`（转正）、`previous = 旧 current`，而旧的 `previous` 被留在了 `swap` 里，随其离开作用域而被 drop（Arc 引用计数减 1）。这是一段零分配的「滑动」实现。

轮转完成后调用 `C::key_confirmed` 通知上层（例如停掉重握手定时器），再设置 `enc_key`，最后 `send_staged` 补发暂存包。

> 小细节：`confirm_key` 只改 KeyWheel 和 `enc_key`，**不碰 recv 表**。因为新 `current`（原 next）和保留为 `previous` 的旧 current 的 `DecryptionState` 在 `add_keypair` 时就已经插入 recv 表了，确认动作不需要再动它们。

#### 4.5.4 代码实践

**实践目标**：把 `confirm_key` 的三步 swap 手算一遍，验证它确实是「next 升 current、current 降 previous」。

**操作步骤**：

1. 假设初始 `next=N, current=C, previous=P`。
2. 逐步代入 [peer.rs:335-338](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L335-L338) 的三条 `mem::swap`。

**需要观察的现象**：照着 4.5.3 的表格，每一步的 `swap` 临时变量都「接过」被清空槽位的旧值，再把它喂给下一格。

**预期结果**：最终 `current=N`、`previous=C`、`next=None`，旧 `P` 被丢弃。你应能解释为什么用三次 swap 而不是简单赋值——为了在不额外分配的情况下正确 drop 被挤出的旧密钥。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `confirm_key` 开头要检查 `Arc::ptr_eq(next, keypair)`，不等就直接返回？
**答案**：防止把一把「已被新一轮握手替换掉」的旧密钥误转正。`keypair` 来自某个 `DecryptionState`，而该状态对应的密钥可能已因新一轮 `add_keypair` 被挤出 `next` 槽。此时若仍推进轮转，会把一把过时密钥升格为 current，破坏 KeyWheel 状态。所以只有当 `next` 仍恰好是这把待确认密钥时才转正。

**练习 2**：`confirmed.swap(true, SeqCst)` 为什么用 `SeqCst`？
**答案**：首报文确认需要与并行/串行阶段的其它原子操作建立严格的先后关系（密钥确认、防回放更新、端点更新的可见性顺序）。`SeqCst` 提供最强的一致性保证，确保「确认」这件事在全序里只发生一次、且其副作用（`confirm_key` 改 KeyWheel）对后续报文可见。在 WireGuard 这类安全敏感路径上，宁可付出微小性能代价也要消除弱序带来的竞态歧义。

---

### 4.6 发送路径：send / send_staged / staged_packets 暂存

#### 4.6.1 概念说明

现在看「密钥怎么被用起来」。出站路径上，`Peer::send` 是加密发送的入口（被 `Device::send` → `peer.send(msg, true)` 调用，见 u5-l2、u3-l2）。它要处理三种情况：

1. **有可用密钥**：分配 nonce、创建 `SendJob`、推入保序队列、扇出给工作线程加密发送。
2. **无密钥**（`enc_key` 为 None）：把包暂存进 `staged_packets`，触发 `need_key` 回调让握手层发起握手。
3. **密钥过期**（nonce 接近 `REJECT_AFTER_MESSAGES`）：清空 `enc_key`，同样暂存 + `need_key`。

`staged_packets` 是一个有界环形队列（`ArrayDeque` + `Wrapping`），容量 `MAX_QUEUED_PACKETS = 1024`。握手成功后由 `send_staged` 把攒下的包按序补发。

#### 4.6.2 核心流程

```
Peer::send(msg, stage):
  锁 enc_key
    None → 若 stage：msg 入 staged_packets；need_key=true
    Some(state):
      state.nonce >= REJECT_AFTER_MESSAGES-1 → 密钥过期
          enc_key=None；若 stage：暂存；need_key=true
      否则：
          job = SendJob::new(msg, nonce, keypair, peer)
          if outbound.push(job) 成功：nonce+=1；job=Some
          否则（队列满）：job=None（丢弃，背压）
  解锁
  if need_key: C::need_key(opaque)        # 触发握手
  if job: device.work.send(Outbound(job)) # 扇出加密
```

`send_staged` 则循环把 `staged_packets` 里的包逐个 `send(msg, false)` 补发（注意第二个参数 `false`——补发时若又没密钥，不再二次暂存，避免无限循环）。

#### 4.6.3 源码精读

`Peer::send` 的三个分支：

[src/wireguard/router/peer.rs:252-298](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L252-L298) — `None` 分支与「密钥过期」分支都走暂存 + `need_key`；正常分支分配 nonce 并创建 `SendJob`，`outbound.push` 失败则静默丢包（背压）。

过期检查的阈值（nonce 计数上限）：

[src/wireguard/router/peer.rs:265-267](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L265-L267) — `if state.nonce >= REJECT_AFTER_MESSAGES - 1` 时判定密钥过期。`REJECT_AFTER_MESSAGES = u64::MAX - 16`（[constants.rs:5](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/constants.rs#L5)），即单密钥最多加密约 \(2^{64}-17\) 个报文。实际远在 `REKEY_AFTER_MESSAGES = 2^{60}`（[constants.rs:4](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/constants.rs#L4)）处就由定时器主动轮换，这里只是最后的安全阀。

`need_key` 回调与扇出：

[src/wireguard/router/peer.rs:288-297](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L288-L297) — `C::need_key(&self.opaque)` 通知上层「我没有密钥了，请发起握手」；有 job 则 `device.work.send(JobUnion::Outbound(job))` 把加密任务扇出给工作线程池。

`send_staged` 的补发循环：

[src/wireguard/router/peer.rs:301-314](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L301-L314) — 循环 `pop_front` 取出暂存包，逐个调 `self.send(msg, false)`。第二个参数 `false` 意味着「补发途中若再无密钥也不再暂存」。

`staged_packets` 的容量与类型：

[src/wireguard/router/peer.rs:43](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L43) — `staged_packets: Mutex<ArrayDeque<[Vec<u8>; MAX_QUEUED_PACKETS], Wrapping>>`，`Wrapping` 策略意味着队列满后再入会**覆盖最旧的包**（而非阻塞或报错）。`MAX_QUEUED_PACKETS` 见 [router/constants.rs:3](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/constants.rs#L3)（= 1024）。

> `send_keepalive` 是 `send` 的一个特例：发一个只有 `SIZE_MESSAGE_PREFIX` 长度的空包（[peer.rs:500-503](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L500-L503)）。它既用于保活，也在 4.4 里被 initiator 用来「主动确认」新密钥。

#### 4.6.4 代码实践

**实践目标**：跟踪「无密钥 → 暂存 → 握手 → 补发」的完整闭环。

**操作步骤**：

1. 从 [device.rs:181-201](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/device.rs#L181-L201) 的 `Device::send` 出发，它调用 `peer.send(msg, true)`。
2. 假设此时 `enc_key` 为 `None`，跟踪到 [peer.rs:257-262](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L257-L262)：包进 `staged_packets`，`need_key=true`。
3. `need_key` 触发握手（u4-l6），握手成功后 `add_keypair` 设 `enc_key`，并在 initiator 分支调 `send_staged`（[peer.rs:485](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L485)）补发。
4. 响应方则在 `confirm_key` 末尾 `send_staged`（[peer.rs:348](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L348)）补发。

**需要观察的现象**：数据包在握手期间不会丢失，而是暂存在 `staged_packets`，密钥就绪后被 `send_staged` 按序补发。

**预期结果**：你能画出 `Device::send → Peer::send(暂存) → need_key → 握手 → add_keypair/confirm_key → send_staged → Peer::send(加密发送)` 的闭环。

#### 4.6.5 小练习与答案

**练习 1**：`send_staged` 里调用 `self.send(msg, false)`，为什么第二个参数是 `false`？
**答案**：`false` 表示「若再次没有密钥，不要把包重新暂存」。补发动作本身就是因为密钥刚就绪才触发的，若补发时又没密钥（比如刚就绪的密钥又被某种竞态清空），再把包塞回 `staged_packets` 会形成无限循环。`false` 让补发途中无密钥则直接走丢包路径，由上层重新触发握手。

**练习 2**：`staged_packets` 用 `Wrapping` 策略，队列满时会发生什么？这样设计合理吗？
**答案**：队列满（超过 `MAX_QUEUED_PACKETS=1024`）后再入队会**覆盖最旧**的包。这在「长时间无密钥」的异常场景下是合理的：与其无限制堆积导致内存膨胀，不如丢弃最旧的包。一旦密钥就绪，`send_staged` 会把仍存活的最新 1024 个包补发出去，尽量保鲜。对实时 VPN 流量而言，丢老包比无限堆积更可接受。

---

### 4.7 生命周期收尾：Drop 清理与 zero_keys

#### 4.7.1 概念说明

peer 被删除时（配置层 drop 掉 `PeerHandle`，比如 UAPI `remove peer`），必须把它的所有痕迹从设备里擦干净：

1. 从 cryptokey 路由表里移除该 peer 的所有 allowed-ips（否则后续报文还会路由到一个已失效的 peer）。
2. 从 recv 表里删除该 peer 所有密钥的 `DecryptionState`（释放 receiver id）。
3. 清空 KeyWheel 与 `enc_key`、`endpoint`（清零密钥材料）。

`zero_keys` 是一个「半清理」操作：只清密钥相关状态、不动路由表，用于 `down()`（设备/peer 暂停）。而 `Drop` 是「全清理」——路由表也一起删。

#### 4.7.2 核心流程

```
Drop for PeerHandle:
  device.table.remove(peer)              # 删路由表项
  收集 next/current/previous 的 recv.id
  recv.write().remove(每个 id)           # 删解密状态
  keys.next=current=previous=None        # 清 KeyWheel
  enc_key=None; endpoint=None            # 清加密状态与端点

zero_keys（用于 down）:
  把 next/current/previous 的 local_id 记入 retired
  recv.write().remove(每个 id)
  keys 三槽置 None
  enc_key=None
  # 注意：不动 table（路由表保留），retired 累积待下次 add_keypair 返回
```

#### 4.7.3 源码精读

`Drop for PeerHandle` 的完整清理：

[src/wireguard/router/peer.rs:144-185](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L144-L185) — 先 `device.table.remove(peer)` 删路由项，再收集三槽的 `recv.id`、从 recv 表删除，最后清空 KeyWheel、`enc_key`、`endpoint`。

> 注意这里收集 id 用的是 `k.recv.id`，与 `zero_keys` 里的 `k.local_id()`（= `recv.id`，见 [types.rs:52-56](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/types.rs#L52-L56)）等价，两种写法指向同一个值。

`zero_keys` 与 `down`/`up`：

[src/wireguard/router/peer.rs:383-418](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L383-L418) — `zero_keys` 把三槽 id 记入 `retired`、从 recv 表删除、三槽与 `enc_key` 置 `None`；`down()` 直接调 `zero_keys()`；`up()` 是空实现。

> `zero_keys` 与 `Drop` 的关键区别：`zero_keys` **不删路由表**（`table` 保留），且把退役 id **累加进 `retired`** 而非直接交还——下次 `add_keypair` 时这些 id 会随返回值交还握手层。而 `Drop` 直接删路由表、清一切，因为 peer 整个没了。

密钥材料的安全清零由 `Key::Drop` 保证（u7-l2 详述）：

[src/wireguard/types.rs:11-16](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/types.rs#L11-L16) — `Key` 实现 `Drop`，drop 时 `self.key.clear()` 把 32 字节密钥清零。所以当 KeyWheel 三槽被置 `None`、`Arc<KeyPair>` 引用计数归零时，其内部的 `send`/`recv` Key 会自动清零，密钥不会残留在内存里。

端点与 allowed-ips 的管理方法（供配置层 UAPI 调用）：

[src/wireguard/router/peer.rs:363-380](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L363-L380) — `set_endpoint`/`get_endpoint` 维护对端地址（注意 `set_endpoint` 的注释：手动设端点时 sticky socket 应被「松开」，见 u2-l3）。

[src/wireguard/router/peer.rs:519-540](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L519-L540) — `add_allowed_ip`/`list_allowed_ips`/`remove_allowed_ips` 操作 cryptokey 路由表，`remove_allowed_ips` 用于 UAPI 的 `replace_allowed_ips=true`。

#### 4.7.4 代码实践

**实践目标**：对比 `Drop` 与 `zero_keys` 的清理范围，理解「删除 peer」与「暂停 peer」的区别。

**操作步骤**：

1. 对照 [peer.rs:144-185](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L144-L185)（Drop）与 [peer.rs:383-412](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L383-L412)（zero_keys）。
2. 列一张两列对照表：哪些操作 Drop 做了而 zero_keys 没做，反之亦然。

**需要观察的现象**：

| 操作 | Drop for PeerHandle | zero_keys |
|------|---------------------|-----------|
| 删路由表 `table.remove` | ✅ | ❌ |
| 删 recv 表项 | ✅（直接 remove） | ✅（直接 remove） |
| 清 KeyWheel 三槽 | ✅ | ✅ |
| 清 `enc_key` | ✅ | ✅ |
| 清 `endpoint` | ✅ | ❌ |
| 退役 id 去向 | （peer 没了，无需归还） | 记入 `retired`，待下次 `add_keypair` 归还 |

**预期结果**：你能解释「`down()` 调 `zero_keys` 保留路由与端点、只清密钥，便于 `up()` 后快速恢复；而 Drop 是彻底拆除」。

#### 4.7.5 小练习与答案

**练习 1**：为什么 `Drop` 要先删 recv 表项、再清 KeyWheel，而不是反过来？
**答案**：顺序上二者在同一函数内、且 recv 表用的是独立的 `device.recv` 写锁，与 `keys` 锁不嵌套，技术上先后都能完成。但逻辑上先收集 id（读 KeyWheel）、再删 recv、最后清 KeyWheel，保证「先释放共享资源（recv 表是设备级、跨 peer 共享）、再清私有状态」，降低持锁期间对其它 peer 的阻塞。更重要的是先 `table.remove`——路由表是出站热路径，先摘除路由项可让后续报文不再尝试发往这个正在被拆除的 peer。

**练习 2**：`zero_keys` 把退役 id 记入 `retired` 而非立刻归还，这些 id 什么时候真正释放？
**答案**：在下次 `add_keypair` 时释放。`add_keypair` 开头 `let mut release = mem::replace(&mut keys.retired, vec![]);`（[peer.rs:443](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L443)）把 `retired` 取出并入返回值，握手工作线程再 `device.release(id)` 归还握手层。也就是说 `down` 后 id 不立即归还，而是「记账」，等下次握手注入新密钥时一并归还。

---

## 5. 综合实践

把本讲知识串起来，完成下面这个「KeyWheel 全生命周期」状态转移图任务。这是本讲规格要求的实践。

**任务**：绘制一张 KeyWheel 状态转移图，覆盖一次完整的密钥轮换，要求标注：

1. **初始态**：peer 刚创建，`next=current=previous=None`（对应 [new_peer](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L187-L213)）。
2. **响应方收到首把密钥**（`add_keypair` 非 initiator 分支）：新密钥入 `next`，`current`/`previous` 仍为 None。
3. **响应方收到首个数据报文**（`confirm_key`）：`next` 转正为 `current`，`enc_key` 首次被设置。
4. **发起方收到 Response**（`add_keypair` initiator 分支）：新密钥直接入 `current`、旧 `current` 降为 `previous`，`enc_key` 立即更新并主动发包确认。
5. **密钥过期**（`Peer::send` 里 `nonce >= REJECT_AFTER_MESSAGES-1`）：`enc_key` 置 None，触发 `need_key` 重握手。
6. **peer 被删除**（`Drop for PeerHandle`）：路由表、recv 表、KeyWheel、enc_key、endpoint 全部清空。

**操作步骤**：

1. 用纸笔或绘图工具画 6 个状态节点，用带标注的箭头连接。
2. 每个箭头上注明触发事件与对应的源码函数（`add_keypair` / `confirm_key` / `send` / `Drop`）。
3. 在 initiator 分支的箭头旁标「立即发包确认（send_staged 或 send_keepalive）」，在非 initiator 分支旁标「等首报文 confirm_key」。
4. 在 `previous` 槽出现的节点旁标「仍可解密旧报文」。

**预期结果**：一张能完整解释「密钥从无到有、从 next 到 current、从 current 到 previous、最终退役」全过程的图。你能指着图回答：什么时候 `enc_key` 被设置？什么时候密钥进 `next` 而非 `current`？`previous` 为什么存在？peer 删除时哪些表被清理？

**待本地验证**：若想用真实数据验证，可参考 [src/wireguard/router/tests/tests.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/tests/tests.rs) 中 `dummy_keypair(true/false)` 配对注入密钥的测试，用 `RUST_LOG=trace cargo test` 观察日志里 `peer.add_keypair`、`peer.confirm_key`、`peer.send_staged` 的触发顺序。

## 6. 本讲小结

- **三层句柄**：`PeerInner` 是真实状态、`Peer` 是可克隆共享引用、`PeerHandle` 是不可克隆所有权句柄（drop 即删除 peer）。
- **KeyWheel 三槽**：`current` 用于加密、`previous` 用于解密上一代飞在路上的报文、`next` 存响应方待确认的新密钥；轮转是「高位 → 低位」的单向流动。
- **收发状态不对称**：发送只需一个 `EncryptionState`（单密钥 + nonce 计数，存于 peer），接收需多个 `DecryptionState`（按 receiver id 存于设备级 recv 表，含防回放位图）。
- **add_keypair 双分叉**：initiator 密钥进 `current` 并立即发包确认、非 initiator 密钥进 `next` 等待静默确认；二者都同步更新 recv 表，并把退役 id 返回握手层 release。
- **confirm_key 静默确认**：响应方收到首个数据报文时，靠 `confirmed.swap` 触发一次 `confirm_key`，用三次 `mem::swap` 把 `next` 提升为 `current`。
- **staged_packets 兜底**：无密钥或密钥过期时数据包暂存（容量 1024，Wrapping 覆盖），密钥就绪后由 `send_staged` 补发，保证握手期间数据不丢。
- **Drop 全清理 / zero_keys 半清理**：Drop 删路由表+recv 表+KeyWheel+enc_key+endpoint；`down()` 调 zero_keys 只清密钥、保留路由与端点。密钥材料由 `Key::Drop` 自动清零。

## 7. 下一步学习建议

本讲把「密钥在路由器里的生命周期」讲完了。接下来建议：

- **u5-l7（防回放窗口 RFC 6479）**：本讲多次提到 `DecryptionState` 里的 `AntiReplay`，下一讲专门精读 `anti_replay.rs` 的滑动位图实现，搞清 `update` 如何在保序串行阶段保证报文不因乱序被误丢。
- **u7-l1（定时器状态机）**：本讲的「密钥过期」「need_key 触发握手」都由上层定时器驱动。阅读 `wireguard/timers.rs` 里 `keep_key_fresh` 与五个定时器，理解 `REKEY_AFTER_*` 如何主动触发本讲的密钥轮换。
- **u7-l2（密钥材料清零）**：本讲提到 `Key::Drop` 清零，u7-l2 会系统讲解 `clear_stack_on_return_fnone`、`State::Drop` 等多层清零机制，把「密钥不残留」的安全设计讲透。

如果想立刻动手验证，可以回到 [src/wireguard/router/tests/tests.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/tests/tests.rs) 与 [bench.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/tests/bench.rs)，用 `dummy_keypair` 注入密钥，配合 `RUST_LOG=trace` 观察 KeyWheel 的真实轮转日志。
