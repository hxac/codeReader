# 握手 Device 与 peer 管理

## 1. 本讲目标

本讲进入握手模块的「调度中枢」——`src/wireguard/handshake/device.rs` 中的 `Device<O>`。它在 u4-l1（报文线路格式）和 u4-l3（Noise IK 密码学计算）之间承上启下：既不自己拼字节、也不自己算密码，而是负责**管理所有 peer、维护本地密钥、分配 receiver id，并把每一条入站握手报文分用（de-multiplex）到正确的密码学函数**。

学完本讲你应当能够：

- 说出 `Device<O>` 四个字段（`keyst`/`pk_map`/`id_map`/`limiter`）各自的作用，并解释为何需要「公钥 ↔ receiver id 双向映射」。
- 解释 `set_sk` 改私钥时为什么要遍历所有 peer 重算 DH 共享密钥、并释放所有进行中的握手 id。
- 读懂 `allocate()` 用「拒绝采样（rejection sampling）」分配 32 位 receiver id 的循环，以及它如何借助 `DashMap` 的 `entry` API 在并发下保证唯一。
- 跟着 `process()` 的 `match` 看清三类握手消息的分用入口，以及 `Output` 三元组的含义。

## 2. 前置知识

- **握手状态机（handshake state machine）**：WireGuard 用一次往返（initiation + response）协商出对称会话密钥。`Device` 就是托管这次往返的状态容器。
- **receiver id（接收方标识）**：握手报文在线路上不携带完整 32 字节公钥，而是用一个随机的 32 位整数（`f_receiver`/`f_sender`）指代「这次握手」。它是临时的、一次性的，握手结束即作废。
- **DH 共享密钥（shared secret, ss）**：本端私钥 `sk` 与对端公钥 `pk` 做 X25519 椭圆曲线运算得到的 32 字节，记作 `DH(sk, pk)`。它在 Noise IK 里是握手的基础，可以提前算好。
- **不透明类型（opaque type `O`）**：`Device<O>` 用泛型 `O` 让握手层「记住」每个 peer 对应的上层对象，却不依赖上层类型。在胶水层里 `O` 被实例化为路由器 peer 句柄（详见 u3-l1）。
- **并发容器**：标准库的 `HashMap` 不是线程安全的；`RwLock<HashMap>` 给整张表加一把读写锁，写者之间必须串行；`DashMap` 则把表**分片（shard）**成多张内部小表，每片各自带锁，写不同片的线程可以真正并行。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/wireguard/handshake/device.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs) | 本讲主角：`Device<O>` 的全部字段与方法（密钥维护、peer 增删、id 分配、`begin`/`process`）。 |
| [src/wireguard/handshake/peer.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/peer.rs) | 单个 peer 的握手状态：`Peer<O>` 结构、`State` 枚举、`reset_state`、`check_replay_flood`。 |
| [src/wireguard/handshake/types.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/types.rs) | 错误类型 `ConfigError`/`HandshakeError`、返回类型 `Output`、`Psk` 别名。 |
| [src/wireguard/handshake/mod.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/mod.rs) | 握手模块入口：声明各子模块、对外 `pub use Device` 与消息常量。 |

## 4. 核心概念与源码讲解

### 4.1 Device 的字段与「公钥 ↔ receiver id」双向映射

#### 4.1.1 概念说明

`Device` 要回答两个方向的问题：

- **发送方向**：本机主动发起握手时，我们知道对端的**公钥**，需要找到对应的 peer 状态。
- **接收方向**：收到一条握手报文时，报文里只有 4 字节的 **receiver id**（参见 u4-l1 的线路格式），我们需要把它翻译回 peer。

于是 `Device` 维护了两张表，构成「公钥 ↔ receiver id」的双向映射：

- `pk_map`：公钥 → peer（方向：已知公钥找 peer）。
- `id_map`：receiver id → 公钥（方向：已知 id 翻译成公钥，再去 `pk_map` 找 peer）。

为什么不让 `id_map` 直接指向 peer？因为 id 是临时、可回收的，而 peer 生命周期更长；用 id→公钥→peer 两跳查找，让 id 表只存「轻量」的 32 字节公钥，回收与遍历都更简单（见 4.3 的 `remove` 与 4.4 的 `release`）。

#### 4.1.2 核心流程

`Device<O>` 的字段定义如下：

[device.rs:38-43](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L38-L43) —— 定义 `Device<O>` 的四个字段，其中 `id_map` 是并发 `DashMap`，其余为单线程结构：

```rust
pub struct Device<O> {
    keyst: Option<KeyState>,
    id_map: DashMap<u32, [u8; 32]>, // concurrent map
    pk_map: HashMap<[u8; 32], Peer<O>>,
    limiter: Mutex<RateLimiter>,
}
```

- `keyst`：本端静态密钥状态。`None` 表示尚未配置私钥，此时设备「空转」（`process` 直接返回 noop）。
- `id_map`：receiver id → 公钥，是**唯一**会被多个握手工作线程并发读写的表，故用 `DashMap`。
- `pk_map`：公钥 → peer。配置期间才改动，受外层 `RwLock` 保护（见文件头注释），方法签名多为 `&mut self`。
- `limiter`：抗 DoS 的令牌桶限速器（详见 u4-l4），用 `Mutex` 包裹。

`keyst` 内含三样东西，见 [device.rs:29-33](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L29-L33)：本端私钥 `sk`、派生公钥 `pk`、以及用于校验入站报文 `mac1`/`mac2` 字段的 `macs::Validator`。

```rust
pub struct KeyState {
    pub(super) sk: StaticSecret, // static secret key
    pub(super) pk: PublicKey,    // static public key
    macs: macs::Validator,       // validator for the mac fields
}
```

> 补充：`MAX_PEER_PER_DEVICE = 1 << 20`（见 [device.rs:27](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L27)），即单设备最多约 100 万 peer，由 `add` 强制。

#### 4.1.3 源码精读

`Device::new` 给出最朴素的初值（[device.rs:97-104](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L97-L104)）：两张表都空，限速器新建，`keyst = None`。

文件头 [device.rs:92-94](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L92-L94) 的注释点明了 `pk_map` 的并发模型：配置（`&mut self` 方法）需要外层 `RwLock` 持有写锁，而 `id_map` 因为要在握手中并发，自己用 `DashMap` 解决。这正是 `id_map` 与 `pk_map` 选不同容器的原因。

#### 4.1.4 代码实践

**实践目标**：从字段类型推断线程安全边界。

1. 打开 [device.rs:38-43](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L38-L43)。
2. 对每个字段，回答：它是「内部自带同步原语」还是「靠 `&mut self` 方法签名来保证独占」？
3. 观察 `begin`/`process`/`release`/`allocate` 的签名都只取 `&self`，而 `add`/`remove`/`set_sk`/`set_psk` 取 `&mut self`。

**预期结果**：`id_map`、`limiter` 自带锁，故能从 `&self` 并发访问；`keyst`、`pk_map` 无锁，只能由 `&mut self`（外层写锁）保护。这一分野决定了「热路径（握手）并发、配置串行」的整体设计。

#### 4.1.5 小练习与答案

**练习 1**：若把 `id_map` 也改成普通 `HashMap`，会发生什么？
**答案**：`allocate`/`release`/`lookup_id` 在握手工作线程里被并发调用（见 u3-l3、u4-l6），普通 `HashMap` 并发读写会触发数据竞争（运行时 panic 或内存破坏），必须加锁或换并发容器。

**练习 2**：为什么 `pk_map` 的 key 用 `[u8; 32]` 而不是 `PublicKey`？
**答案**：注释 [device.rs:59-63](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L59-L63) 指出 `PublicKey` 没有实现 `Hash`，因此用其底层的 32 字节数组作为可哈希的 key，`Device` 的包装方法（`get`/`contains_key`/`iter`）把这个细节对外隐藏。

### 4.2 私钥与共享密钥的维护：set_sk / update_ss

#### 4.2.1 概念说明

Noise IK 握手的每一次往返都依赖一个**预计算的 DH 共享密钥** `ss = DH(本端私钥, 对端公钥)`（详见 u4-l3）。因为 `ss` 只与「本端私钥 + 对端公钥」有关，而与具体某次握手无关，所以可以在添加 peer 时就算好、存进 `Peer.ss`，握手时直接取用，省掉每次握手的标量乘法。

一旦本端私钥被改（`set_sk`），所有 peer 的 `ss` 都失效，必须重算——这就是 `update_ss` 存在的原因。

#### 4.2.2 核心流程

`set_sk`（[device.rs:134-156](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L134-L156)）分四步：

1. 由新私钥构造 `KeyState`（派生公钥、新建 `Validator`），存入 `keyst`；传 `None` 则清空密钥。
2. 调 `update_ss()` 重算每个 peer 的 `ss`，并收集「被中止的握手 id」与「与设备公钥撞车的 peer」。
3. 用 `release(id)` 把收集到的 id 还回池子（见 4.4）。
4. 若发现某 peer 公钥 == 设备公钥，从 `pk_map` 删掉并返回它（不能和自己握手）。

`update_ss`（[device.rs:106-127](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L106-L127)）对每个 peer：

```text
若 keyst 存在:
    若 peer.pk == device.pk:  记为 same，peer.ss 清零
    否则:                     peer.ss = DH(sk, peer.pk)
否则:
    peer.ss 清零
若 peer.reset_state() 返回 Some(id):  收集 id（该 peer 有进行中的发起被中止）
```

#### 4.2.3 源码精读

`update_ss` 的循环体（[device.rs:109-124](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L109-L124)）展示了「重算 ss + 中止进行中握手」两件事的耦合。关键调用 `peer.reset_state()`（见 4.4 与 peer.rs）会把处于 `InitiationSent` 的 peer 拍回 `Reset`，并交还它占用的 receiver id——这个 id 此时已无效，必须释放，否则 `id_map` 会残留指向已作废握手的映射。

`set_sk` 的撞车检测（[device.rs:181-185](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L181-L185) 在 `add`、[device.rs:111-113](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L111-L113) 在 `update_ss`）防止本机把自己的公钥配成 peer，那会产生「跟自己握手」的无意义密钥协商。

#### 4.2.4 代码实践

**实践目标**：观察改私钥的副作用。

1. 阅读 [tests.rs 的 setup_devices](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/tests.rs#L17-L50)：先 `set_sk` 再 `add`，因此 `add` 时已能算出 `ss`。
2. 设想把顺序反过来：先 `add`（此时 `keyst=None`，`ss` 为全零占位）再 `set_sk`。`set_sk` 内部的 `update_ss` 会补算 `ss`。
3. 在 [device.rs:116](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L116) 加一行 `log::trace!("recomputed ss for peer");`（示例代码），运行握手测试观察触发次数。

**预期结果**：`add` 时若已有私钥，`ss` 当场算好，`set_sk` 的 `update_ss` 不再为其补算；反之 `update_ss` 会遍历到它。

#### 4.2.5 小练习与答案

**练习 1**：`update_ss` 为什么要返回 `(Vec<u32>, Option<PublicKey>)` 而不是直接在函数里 `release`/`remove`？
**答案**：`release` 需要 `&self` 而 `remove` 需要 `&mut self`；`update_ss` 已经借了 `self.pk_map.iter_mut()`（可变借用），此时无法再调 `self` 的方法。所以先把结果收集出来，等循环结束、借用释放后，由调用方 `set_sk`（[device.rs:146-155](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L146-L155)）再执行 `release` 与 `remove`。这是典型的「借用冲突迫使拆分两段」。

**练习 2**：把本端私钥清空（`set_sk(None)`）后，已建立的 `ss` 会怎样？
**答案**：`update_ss` 走 `else` 分支把每个 peer 的 `ss` 清零（[device.rs:118-120](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L118-L120)），此后任何握手都因 `ss` 为零而被拒绝（见 u4-l3 的零共享密钥检查）。

### 4.3 peer 的增删与 PSK：add / remove / set_psk / get_psk

#### 4.3.1 概念说明

这一组方法管理 `pk_map` 的内容，是配置层（UAPI 的 `set` 操作，见 u6-l3）落到握手层的最终入口。它们都取 `&mut self`，说明 peer 增删是「配置期」操作，不在握手热路径上。

- `add`：添加一个 peer，预计算 `ss`。
- `remove`：删除一个 peer，并清扫它在 `id_map` 里的残留。
- `set_psk` / `get_psk`：设置/读取该 peer 的 32 字节预共享密钥（PSK），PSK 会混入 Noise IK 的 `psk2` 步骤（u4-l3），为握手增加一道带外认证。

#### 4.3.2 核心流程

`add`（[device.rs:174-201](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L174-L201)）：

```text
若 peer 数 > 2^20:        报 ConfigError("Too many peers")
若 peer.pk == device.pk:  报 ConfigError("Public key of peer matches the device")
否则:                     ss = keyst 存在 ? DH(sk, pk) : [0;32]
                          pk_map.insert(pk, Peer::new(pk, ss, opaque))
```

`remove`（[device.rs:213-223](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L213-L223)）：先从 `pk_map` 删（找不到则报错），再 `id_map.retain(|_, v| v != pk)` 把该 peer 所有 id 条目扫掉。注释坦言这是 O(n) 全表扫描，但「只在删 peer 时发生，罕见」。

`set_psk` / `get_psk`（[device.rs:235-261](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L235-L261)）：直接读写 `peer.psk`，找不到公钥返回 `ConfigError("No such public key")`。

#### 4.3.3 源码精读

`Peer::new`（[peer.rs:62-72](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/peer.rs#L62-L72)）把 peer 初始化为「全 Reset」状态：`state = Reset`、`timestamp = None`、`psk = [0;32]`（默认无 PSK）、`macs` 用对端公钥新建一个 `Generator`。

`Psk` 类型见 [types.rs:90](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/types.rs#L90)，就是 `[u8; 32]` 的别名。

注意 `remove` 的「两张表一致性」：`pk_map` 删除后，对应的 `id_map` 条目若不清理就会变成「指向不存在公钥」的悬空映射。虽然 `lookup_id` 里写了 `unreachable!()`（[device.rs:453-456](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L453-L456)）依赖「id 在 id_map 里 ⇒ pk 必在 pk_map 里」这一不变式，`remove` 的 `retain` 正是维护该不变式。

#### 4.3.4 代码实践

**实践目标**：用一个 proptest 复现 `unique_shared_secrets` 的思路。

阅读 [device.rs:487-514](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L487-L514) 自带的属性测试：它随机生成私钥与两个公钥，`set_sk` 后 `add`，再断言「每个 peer 的 `ss` 互不相同」。试着把它扩展为 N 个 peer 的版本，断言 `HashSet` 长度等于 peer 数。

**预期结果**：由于 `DH(sk, pk_i)` 对不同 `pk_i` 几乎必然得到不同结果（曲线性质），不同 peer 的 `ss` 几乎总是不同。

#### 4.3.5 小练习与答案

**练习**：`remove` 为什么不能用 `release` 的方式逐个删 id，而要 `retain` 全表扫描？
**答案**：`release` 一次只删一个已知 id；但 `remove` 时我们并不知道该 peer 当前占着哪些 id（id 是握手时动态 `allocate` 的，可能 0 个、1 个或多个）。只有遍历 `id_map`，按 value 等于该公钥来筛除，才能保证删干净。代价是 O(n)，但删 peer 罕见，可接受。

### 4.4 receiver id 的分配与查找：allocate / lookup_id / lookup_pk / release

#### 4.4.1 概念说明

receiver id 是线路上的「短期门票」。本机每发起或响应一次握手，都要 `allocate` 一个全新的 32 位随机 id，存进 `id_map`；对方后续报文用这个 id 寻址回来；握手完成或中止时 `release` 归还。

为什么用「拒绝采样」？因为 id 必须**不可预测**（否则会被指纹识别或定向干扰），所以从 \( 2^{32} \) 空间随机采样；同时又要**与现存 id 不冲突**，所以采到已占用的就丢掉重采。

#### 4.4.2 核心流程

`allocate`（[device.rs:463-478](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L463-L478)）：

```text
loop:
    id = rng.gen::<u32>()              // 随机采样
    若 id_map.contains_key(id): continue  // 快速跳过（读锁/分片读）
    match id_map.entry(id):            // 原子地「占位 or 重试」
        Vacant(e) => { e.insert(pk); return id }
        Occupied(_) => 继续循环
```

冲突概率：当表中已有 \( k \) 个 id 时，单次采样命中冲突的概率约为 \( k / 2^{32} \)。即便 \( k = 10^6 \)（上限），冲突率也仅约 \( 2.3 \times 10^{-4} \)，平均重采次数远小于 2，性能无忧。

`lookup_id`（[device.rs:445-457](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L445-L457)）：id →（`id_map`）→ 公钥 →（`pk_map`）→ peer。两跳查找。

`lookup_pk`（[device.rs:436-440](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L436-L440)）：公钥直接查 `pk_map`。

`release`（[device.rs:268-271](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L268-L271)）：`id_map.remove(id)`，并用 `assert!(old.is_some())` 断言「释放的 id 必然是之前分配过的」——这是内部不变式自检。

#### 4.4.3 源码精读：DashMap 相对 RwLock<HashMap> 的优势

`allocate` 是本讲并发设计的核心。提交 `6e307fc`（`Replace RwLock<HashMap> with DashMap in handshake`）专门改写了它。对比新旧两版（新代码见 [device.rs:463-478](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L463-L478)）：

旧版（`RwLock<HashMap>`）的关键问题是「检查与插入分两次加锁」：

```text
if id_map.read().contains_key(id) { continue }   // 读锁：检查
// ← 这里释放读锁，存在 TOCTOU 窗口
let mut m = id_map.write();                        // 写锁
if !m.contains_key(id) { m.insert(...); }          // 再检查 + 插入
```

旧版靠「写锁里再检查一次」来弥补竞态，但**整张表只有一把写锁**，多个握手线程的 `allocate` 必须串行通过写锁段，成为瓶颈。

新版（`DashMap`）用 `entry(id)` API：它在**单分片**的锁内原子完成「判空 + 插入」，且 DashMap 内部分了 N 片、各片独立加锁，于是不同 id 落到不同分片时可以真正并行分配。一句话总结：`DashMap` 把「一把全局写锁」换成「多把分片锁」，并把「检查 + 插入」收进一次原子的 `entry` 调用，既消除了竞态又提升了并发度。

#### 4.4.4 代码实践

**实践目标**：为 `allocate` 写一个并发测试，验证多线程下分配出的 id 全局唯一（详见第 5 节综合实践的前置步骤）。因为 `allocate` 是私有方法，测试需写在 [device.rs 末尾的 `#[cfg(test)] mod tests`](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L481-L515) 内。

**操作步骤**：

1. 在 `mod tests` 里新建一个 `Device<u32>`，`set_sk(Some(sk))`，`add(pk, 1)`。
2. 用 `std::thread::scope` 启动 8 个线程，每个线程循环 1000 次 `dev.allocate(&mut OsRng, &pk)`，把返回的 id 收进各自的 `Vec<u32>`。
3. 主线程合并所有 id 进一个 `HashSet<u32>`，断言 `set.len() == 8 * 1000`。

**需要观察的现象**：测试稳定通过；`id_map` 中最终有 8000 个条目，无重复 key。

**预期结果**：通过。若把 `DashMap` 换回 `RwLock<HashMap>` 并去掉 `entry` 的原子性（人为制造竞态），理论上可能出现重复插入覆盖、`HashSet` 长度仍等于总数但 `id_map` 条目数偏少——这正是 DashMap 解决的问题。

> 说明：本实践为「源码阅读 + 自行补测试」型，未在此实际运行；具体能否编译取决于测试代码是否放在 `mod tests` 内（否则无法访问私有 `allocate`）。

#### 4.4.5 小练习与答案

**练习 1**：`lookup_id` 里的 `unreachable!()`（[device.rs:453-456](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L453-L456)）依赖哪条不变式？
**答案**：「id_map 的 value（公钥）必然存在于 pk_map」。这条不变式由 `add`（先建 pk_map 条目）、`remove`（同步清理 id_map）、`release`（只删 id_map 不动 pk_map）共同维护。只要不被外部破坏，id 命中就一定能在 pk_map 找到 peer。

**练习 2**：`release` 里的 `assert!(old.is_some(), "released id not allocated")`（[device.rs:269-270](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L269-L270)）若在运行时触发，说明什么？
**答案**：说明释放了一个从未分配的 id，即内部逻辑错乱（例如重复释放、或释放了别处的 id）。这是 debug 期发现「id 生命周期管理 bug」的断言。

### 4.5 握手入口：begin 与 process 的分用

#### 4.5.1 概念说明

`Device` 对外暴露两个握手入口：

- `begin`：本机**主动**发起握手（作为发起方），产出一條 Initiation 报文。
- `process`：本机**被动**处理一条入站握手报文（可能是对方的 initiation、response 或 cookie reply）。

`process` 是 `udp_worker` → `handshake_worker` 链路的终点之一（见 u3-l3、u4-l6），它读出报文首 4 字节的 type 字段，把三类消息分流到对应的 Noise 函数。本讲只看「分用骨架」，密码学细节留给 u4-l3，抗 DoS 细节留给 u4-l4。

#### 4.5.2 核心流程

`begin`（[device.rs:278-301](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L278-L301)）：

```text
keyst 与 peer 都存在时:
    local = allocate(rng, pk)                 // 给本次发起分配 receiver id
    noise::create_initiation(... msg.noise)   // 填充 Noise 内层（u4-l3）
    peer.macs.generate(msg.noise, &mut msg.macs) // 生成 mac1/mac2（u4-l4）
    返回序列化后的 Initiation 字节
否则: 报 UnknownPublicKey
```

`process`（[device.rs:308-431](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L308-L431)）的分用骨架：

```text
若 msg.len() < 4:                      报 InvalidMessageFormat
若 keyst 为 None:                        返回 noop (None,None,None)   // 设备未配私钥
按 LittleEndian::read_u32(msg) 的 type 分支:
    TYPE_INITIATION(1):
        解析 → check_mac1 → (under-load 时) check_mac2/cookie + 限速
        → consume_initiation 得到 (peer, pk, st)
        → allocate 新 id → create_response 得到 keys
        → generate macs
        → 返回 (Some(peer.opaque), Some(resp), Some(keys))  // keys 为未确认 keypair
    TYPE_RESPONSE(2):
        解析 → check_mac1 → (under-load 时) check_mac2/cookie + 限速
        → consume_response → 返回其 Output（含已确认 keypair）
    TYPE_COOKIE_REPLY(3):
        解析 → lookup_id(f_receiver) 找到 peer
        → peer.macs.process(&msg) 更新本地 cookie
        → 返回 (None, None, None)   // 不产生新报文，也不做密码学确认
    _: 报 InvalidMessageFormat
```

返回类型 `Output<'a, O>` 见 [types.rs:82-86](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/types.rs#L82-L86)，是一个三元组：

```rust
pub type Output<'a, O> = (
    Option<&'a O>,   // 命中的 peer 的 opaque（交给上层）
    Option<Vec<u8>>, // 需要回发的报文（None 表示无回复）
    Option<KeyPair>, // 握手成功派生的会话密钥对（交给路由器）
);
```

上层 `handshake_worker`（u4-l6）据此决定：要不要把 `KeyPair` 塞给路由器（u5-l6 的 KeyWheel）、要不要把回复报文发出去。

#### 4.5.3 源码精读

注意 `process` 的几个细节：

- **未配私钥时空转**：[device.rs:321-326](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L321-L326) 直接返回全 `None`，不报错——设备还没 `set_sk` 时，静默丢弃入站握手。
- **under-load 是可选的**：`src: Option<SocketAddr>` 为 `Some` 时才做 mac2/cookie 与限速检查（[device.rs:338-356](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L338-L356)），`None` 时跳过。是否传 `Some` 由 `handshake_worker` 根据 `pending` 计数判定（见 u3-l3、u4-l4）。
- **create_response 失败要回滚 id**：[device.rs:368-372](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L368-L372) 用 `map_err` 在 Noise 失败时 `release(local)`，避免 id 泄漏。
- **CookieReply 不做密码学确认**：[device.rs:416-428](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L416-L428) 只是用它刷新对端的 cookie 状态，不产生 keypair，注释明确「DOES NOT cryptographically verify the peer」。

#### 4.5.4 代码实践

**实践目标**：跟着 `handshake_under_load` 测试走一遍 `process` 的分支。

1. 阅读 [tests.rs:66-80](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/tests.rs#L66-L80)：`dev1.begin(...)` 造 initiation → `dev2.process(..., Some(src1))`。
2. 因为带 `Some(src)` 且 dev2 此刻没有有效 cookie，`process` 走 TYPE_INITIATION 分支的 `check_mac2` 失败路径，返回 `(None, Some(cookie_reply), None)`。
3. 对照 [device.rs:340-350](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L340-L350) 确认这就是「创建 CookieReply 并返回」的分支。

**预期结果**：测试中 `match ... { (None, Some(msg), None) => msg, _ => panic }` 命中第一条手臂，印证了「under-load 下首条 initiation 被换成 cookie reply」。

#### 4.5.5 小练习与答案

**练习 1**：`begin` 与 `process` 的签名里都带 `R: RngCore + CryptoRng`，这个 rng 用在哪？
**答案**：`allocate` 用它采样新 receiver id；`noise::create_initiation`/`create_response` 用它生成临时密钥对（ephemeral key）等握手所需的随机量。把 rng 作为参数传入而非全局，便于测试注入确定性随机源。

**练习 2**：`process` 处理 TYPE_INITIATION 时返回的 `KeyPair` 与处理 TYPE_RESPONSE 时返回的，确认状态有何不同？
**答案**：TYPE_INITIATION（作为响应方）返回的 keypair 是**未确认（unconfirmed）**的——要等对方发来第一个数据报文才算确认（详见 u5-l6 的 `confirm_key`）；TYPE_RESPONSE（作为发起方收到回复）返回的 keypair 因发起方会立刻发数据而视为已确认。细节在 `noise::create_response`/`consume_response` 与 u4-l3、u5-l6 展开。

## 5. 综合实践

把本讲的知识串起来，完成一个「为 `Device::allocate` 补并发回归测试」的任务。它同时考察 4.1（字段）、4.4（allocate/release/lookup_id）和对 DashMap 并发优势（提交 `6e307fc`）的理解。

**任务**：在 [device.rs 的 `#[cfg(test)] mod tests`](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L481-L515) 中新增测试 `allocate_is_unique_under_concurrency`（示例代码，需你自行补全并放入该模块才能访问私有 `allocate`）：

```rust
// 示例代码：放在 device.rs 的 #[cfg(test)] mod tests 内
use std::collections::HashSet;
use std::thread;

#[test]
fn allocate_is_unique_under_concurrency() {
    let mut dev: Device<u32> = Device::new();
    let sk = StaticSecret::new(&mut rand::rngs::OsRng);
    dev.set_sk(Some(sk));
    let pk = PublicKey::from(dev.get_sk().unwrap()); // 仅用于 add，值不重要
    // 用一个独立公钥添加 peer
    let peer_sk = StaticSecret::new(&mut rand::rngs::OsRng);
    let peer_pk = PublicKey::from(&peer_sk);
    dev.add(peer_pk, 1).unwrap();

    let dev = std::sync::Arc::new(dev);
    let threads = 8;
    let per_thread = 1000;
    let handles: Vec<_> = (0..threads)
        .map(|_| {
            let dev = dev.clone();
            thread::spawn(move || {
                let mut rng = rand::rngs::OsRng;
                let mut ids = Vec::with_capacity(per_thread);
                for _ in 0..per_thread {
                    ids.push(dev.allocate(&mut rng, &peer_pk));
                }
                ids
            })
        })
        .collect();

    let mut all = HashSet::new();
    for h in handles {
        for id in h.join().unwrap() {
            assert!(all.insert(id), "duplicate id allocated across threads");
        }
    }
    assert_eq!(all.len(), threads * per_thread);
}
```

**操作步骤**：

1. 确认测试位于 `mod tests` 内（`use super::*;` 已在 [device.rs:483](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L483) 引入），否则 `allocate` 不可见。
2. 运行 `cargo test --release allocate_is_unique_under_concurrency`（release 下并发更激烈，更容易暴露竞态）。
3. 观察通过后，写一段注释解释：为什么同样的测试在旧版 `RwLock<HashMap>`（全局写锁串行化）下也能通过，但吞吐更低；而 `DashMap` 的分片锁让 8 个线程的 `entry` 调用大部分时间在不同分片上并行。

**需要观察的现象**：测试稳定通过，`HashSet` 长度恰为 8000，`id_map` 无重复 key。

**预期结果**：通过。若想直观对比，可临时把 `id_map` 改回 `RwLock<HashMap>` 并把 `entry` 拆成「read 检查 + write 插入」两步（去掉中间的二次检查），用 `cargo test` 多次运行，有可能偶发地让两个线程同时通过 read 检查、各自 write 同一个 id，从而 `HashSet` 长度仍为 8000（因为返回值本身就是两个相同 id），但 `id_map.len()` 会小于 8000——这就是 DashMap `entry` 原子性所消除的竞态。

> 待本地验证：上述「改回旧实现」的对比实验需要你手动改源码并改回，本讲义不修改任何源码。

## 6. 本讲小结

- `Device<O>` 用四个字段托管握手状态：`keyst`（本端密钥+MAC 校验器）、`pk_map`（公钥→peer）、`id_map`（receiver id→公钥，并发 `DashMap`）、`limiter`（抗 DoS 限速器）。
- 「公钥 ↔ receiver id」双向映射服务于两个方向：发送时按公钥找 peer（`pk_map`），接收时按 4 字节 id 翻译回公钥再找 peer（`id_map`→`pk_map`）。
- `set_sk` 改私钥会触发 `update_ss` 为每个 peer 重算 DH 共享密钥 `ss`，并 `release` 所有被中止握手的 id；撞车（peer 公钥==设备公钥）的 peer 会被剔除。
- `add`/`remove`/`set_psk`/`get_psk` 是配置期的 `&mut self` 方法，`remove` 用 O(n) `retain` 维护「id_map 的 value 必在 pk_map」这一不变式。
- `allocate` 用拒绝采样从 \( 2^{32} \) 空间分配随机 id，借助 `DashMap::entry` 的分片级原子「检查+插入」消除竞态、提升并发——这是提交 `6e307fc` 的核心改进。
- `begin`/`process` 是两个握手入口；`process` 按 type 字段把 Initiation/Response/CookieReply 三类消息分用到对应 Noise 函数，返回 `Output`（opaque、回复报文、派生密钥对）三元组交给上层。

## 7. 下一步学习建议

- **u4-l3（Noise IK 核心）**：本讲把 `create_initiation`/`consume_initiation`/`create_response`/`consume_response` 当黑盒，下一讲打开它们，看 `ss`、`psk` 如何被混进哈希转录并最终派生 `KeyPair`。
- **u4-l4（抗 DoS）**：本讲提到 `check_mac1`/`check_mac2`/`limiter.allow`，下一讲深入 `macs.rs` 的 mac1/mac2 与 cookie、以及 `ratelimiter.rs` 的令牌桶。
- **u4-l5（时间戳与重放）**：本讲涉及 `peer.reset_state`，下一讲读 `peer.rs` 的 `check_replay_flood` 与 `timestamp.rs`，理解发起方洪泛与时间戳重放的防护。
- **u4-l6（握手工作线程）**：看 `handshake_worker` 如何把 `begin`/`process` 的 `Output` 落地为「派发报文 + 交付 KeyPair 给路由器」。
