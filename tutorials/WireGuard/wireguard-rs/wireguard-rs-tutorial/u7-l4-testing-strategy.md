# 测试策略与纯软件回归

## 1. 本讲目标

WireGuard 是一个「密码学正确性一旦出错就等于不安全」的系统。本讲不引入新协议知识，而是回答一个问题：**wireguard-rs 如何在不依赖真实网卡、不需要 root 的情况下，把整条「握手 → 加密 → 路由 → 解密」链路跑起来并验证它是对的？**

学完后你应当能够：

- 看懂 `test_pure_wireguard` 如何用 dummy 平台把两个 WireGuard 实例「背靠背」对接，做端到端纯软件回归。
- 理解 `make_packet` 借助 `pnet` 构造可被 cryptokey 路由表识别的 IPv4/IPv6 测试报文。
- 掌握 `proptest` 属性测试在 cookie（mac1/mac2）与共享密钥唯一性上的用法，以及它和普通单点断言的区别。
- 看懂路由器保序队列 `Queue` 的并发模糊测试（fuzz）如何压测「多生产者、多消费者、恰好一次、按序」这一不变量。
- 能够基于 `test_pure_wireguard` 自己编写一个新的端到端测试，专门验证「大报文 padding 对齐后仍按序送达」。

## 2. 前置知识

本讲是收尾篇，默认你已读完下列讲义，这里只做最简回顾，不重复细节：

- **u2-l4 Dummy 平台**：`platform/dummy` 用 `std::sync::mpsc` 的通道把所有系统调用替换成内存操作，使协议核心 `WireGuard<T, B>` 能在 `cargo test` 里无副作用、无 root 地端到端运行。`TunTest` 模拟虚拟网卡，`PairBind::pair` 模拟一根「虚拟网线」把两个实例的 UDP 收发对接。
- **u3-l1 胶水层**：`WireGuard<T, B>` 把 `handshake::Device` 与 `router::Device` 粘合，`WireGuard::new` 按核数 spawn 握手工作线程，`add_tun_reader`/`add_udp_reader` 为每个 reader spawn 一个工作线程。
- **u3-l2 出站 padding**：`tun_worker` 读出明文 IP 包后用 `padding(payload, mtu)` 对齐到 16 字节、且不超过 MTU，再交给路由器加密发送。这是本讲综合实践的核心。

需要补充的三个通用术语：

- **回归测试（regression test）**：改动代码后重跑一组固定用例，确认旧行为没被改坏。`test_pure_wireguard` 就是 wireguard-rs 最重要的一条回归用例。
- **属性测试（property-based test）**：不写死一两个输入，而是让框架（`proptest`）自动生成大量随机输入，断言某个「性质」对所有输入都成立。例如「任意两把不同公钥，算出的 DH 共享密钥必不相同」。
- **模糊测试 / 压力测试（fuzz / stress）**：多线程并发地对一个数据结构随机调用其方法（push/consume），跑上百万次，目标是触发并发竞态。`Queue` 的保序不变量就是被这样压测的。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [src/wireguard/tests.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/tests.rs) | 顶层端到端测试：`make_packet` 构包、`test_pure_wireguard` 双实例握手+收发回归。 |
| [src/platform/dummy/mod.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/dummy/mod.rs) | dummy 平台总入口，导出 `endpoint`/`tun`/`udp` 子模块。 |
| [src/platform/dummy/tun/dummy.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/dummy/tun/dummy.rs) | `TunTest`/`TunFakeIO`/`TunReader`/`TunWriter`/`TunStatus`，纯内存 TUN。 |
| [src/platform/dummy/udp.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/dummy/udp.rs) | `PairBind::pair` 用两条交叉通道把两个实例对接成「虚拟网线」。 |
| [src/wireguard/handshake/tests.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/tests.rs) | 握手层单元测试：无载握手、过载下的 7 报文最长握手。 |
| [src/wireguard/handshake/macs.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/macs.rs) | cookie 的 `proptest` 属性测试（mac1/cookie/mac2 闭环）。 |
| [src/wireguard/handshake/device.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs) | `proptest` 验证「不同公钥 → 不同共享密钥」。 |
| [src/wireguard/router/tests/mod.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/tests/mod.rs) | 路由器测试公用件：`pad`、`dummy_keypair`、`init`。 |
| [src/wireguard/router/tests/tests.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/tests/tests.rs) | 路由器出站/双向回归，用 `EventTracker` 追踪回调事件。 |
| [src/wireguard/router/queue.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/queue.rs) | 保序队列的 `test_consume_queue`（带睡眠）与 `test_fuzz_queue`（百万次）。 |
| [Cargo.toml](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/Cargo.toml) | `[dev-dependencies]` 声明 `pnet`/`proptest`/`rand_chacha`，只在测试时引入。 |

## 4. 核心概念与源码讲解

### 4.1 test_pure_wireguard：双实例端到端纯软件回归

#### 4.1.1 概念说明

这是整个项目最关键的一条测试。它把两个完整的 `WireGuard<dummy::TunTest, dummy::PairBind>` 实例像两台真实路由器一样「背靠背」连起来，验证三件事：

1. **握手能成功完成**（首个数据包会自动触发一次 Noise IK 握手）。
2. **所有不超过 MTU 的报文都能被送达**。
3. **所有报文都按发送顺序到达**（unmodified and in-order）。

它之所以「纯软件」：两个实例之间没有真实 UDP socket，没有真实 TUN 网卡，全靠 dummy 平台的内存通道相连。因此可以在 CI 里、无 root、无内核模块的环境下跑通。

#### 4.1.2 核心流程

```text
┌─────────── wg1 (TunTest + PairBind) ───────────┐        ┌─────────── wg2 ──────────┐
│ TunFakeIO1 ──tx1──► TunReader1 ──► tun_worker1 │        │ TunFakeIO2 ◄── TunWriter2│
│                  (明文 IP 包注入)               │        │   (解密后写回对端网卡)    │
│                                                │        │                          │
│   tun_worker1 ──► router1 ──加密──► PairWriter1│        │ PairReader2 ──► router2  │
│                                  │             │ 交叉    │   udp_worker2            │
│                                  ▼             │ 通道    │                          │
│   PairReader1 ◄──udp_worker1◄── router1        │ ◄────► │ router2 ◄── PairWriter2  │
│   (解密后写回)                                  │        │                          │
└────────────────────────────────────────────────┘        └──────────────────────────┘
                        PairBind::pair() 用 tx1↔rx2、tx2↔rx1 两条 sync_channel(128) 交叉连接
```

装配顺序（先建 TUN、再连 UDP、再配密钥与路由、最后发包）严格对应 `main.rs` 在真实平台上的装配顺序，只是把「系统调用」换成了「通道」。

#### 4.1.3 源码精读

建两个 dummy TUN 并各自起一个 tun_worker；`wg.up(1500)` 把设备拉起并把 MTU 设为 1500（后续 `tun_worker` 每轮都会原子重载这个 MTU）：

[tests.rs:77-85](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/tests.rs#L77-L85) —— 用 `dummy::TunTest::create(true)` 拿到 `(fake, reader, writer, status)` 四元组，`true` 表示「存储模式」（写出的包会被对端能读到）。

[tests.rs:89-95](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/tests.rs#L89-L95) —— `PairBind::pair()` 造出两对 `(reader, writer)`，分别交给两个实例当 UDP 收发端，等于在它们之间拉了一根虚拟网线。

[tests.rs:115-136](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/tests.rs#L115-L136) —— 关键配置：互加对端公钥、互设本端私钥、互配 allowed-ip（cryptokey 路由表），并给其中一侧设端点（另一侧靠首报文「漫游学习」，见 u5-l3）。

[tests.rs:143-170](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/tests.rs#L143-L170) —— 发包与验收。`fake1.write(p)` 把明文 IP 包「灌进」wg1 的网卡，等价于内核把一个本机 IP 包送进 TUN；随后在 wg2 的 `fake2.read()` 处应能读到**逐字节相同**的包。这里用 `hex::encode` 把两边都编码成十六进制再比较，断言信息明确写着「unmodified and in-order」。

> 注意它的收发配对写法：先把 `packets` 逆序 `pop` 写入，再把克隆出的 `backup` 同样逆序 `pop` 比对。两次逆序相互抵消，等价于断言「第 i 个收到的包 == 第 i 个发出去的包」，即 FIFO 保序——这条不变量最终由路由器的保序 `Queue`（u5-l4）保证。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：理解首包如何「顺带」触发一次握手。
2. **步骤**：在 `test_pure_wireguard` 的 `fake1.write(p)` 之前打一行 `println!`，并对照 [src/wireguard/workers.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs) 的 `tun_worker` → `router.send` → `Peer::send`。
3. **观察现象**：第一个包写入后，因尚无会话密钥，会被推进 `staged_packets` 暂存，并通过 `Callbacks::need_key` 触发握手（参见 u3-l2、u5-l6）。
4. **预期结果**：握手完成后 `send_staged` 把暂存包补发，最终 wg2 仍能按序收到全部 20 个包。运行用 `cargo test --release test_pure_wireguard -- --nocapture`（release 可缩短握手轮询等待）。
5. 运行结果：「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么测试要在两侧分别设 `allowed_ip`（wg1 上 peer2 设 `192.168.2.0/24`，wg2 上 peer1 设 `192.168.1.0/24`），而不是两边都设同一个网段？

**参考答案**：cryptokey 路由是「按地址归属到 peer」的。wg1 要把目的为 `192.168.2.10` 的包发给 peer2，所以 wg1 侧 peer2 必须宣告 `192.168.2.0/24`；反过来 wg2 收到包后要按**源地址** `192.168.1.20` 反查校验，所以 wg2 侧 peer1 必须宣告 `192.168.1.0/24`。两边网段对称、方向相反，正是 u5-l5 讲的「出站按目的、入站按源」双向复用同一张表。

**练习 2**：测试为什么只给一侧 `set_endpoint`，另一侧不设？

**参考答案**：WireGuard 支持无缝漫游——只要有一方知道对方端点就能发出首包；另一方收到首个合法报文后，会从报文源地址学习到对端端点（u5-l3 的端点更新）。所以只需一侧有端点，握手即可启动，省去了双方都要预知地址的麻烦。

---

### 4.2 make_packet：构造可路由的 IP 测试报文

#### 4.2.1 概念说明

要测数据面，就得有「像样的 IP 包」喂进去。`make_packet` 用 [`pnet`](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/Cargo.toml#L51-L54) crate（仅 dev-dependency）构造一个字段合法的 IPv4/IPv6 报文，载荷是用确定性 PRNG 填充的伪随机字节。它构造的报文必须满足两点，才能被路由器接受：

- **版本字段正确**（IPv4 的 `version=4`、IPv6 的 `version=6`），路由器据此分流（u5-l5）。
- **总长度字段正确**，接收端靠它（`inner_length`）剥掉尾部 padding 与 AEAD 标签还原原始包。

载荷用 `ChaCha8Rng::seed_from_u64(id)` 生成，是**确定性**的：同一个 `id` 永远产出同一份字节，这样收发两侧都能重现并比对。

#### 4.2.2 核心流程

```text
make_packet(size, src, dst, id):
  1. ChaCha8Rng::seed_from_u64(id) → 填充 size 字节随机载荷 p
  2. 若 dst 是 IPv4:
        length = size + 20          # 20 = IPv4 最小头
        建 MutableIpv4Packet，设 src/dst/total_length/version(4)，写入 p
  3. 若 dst 是 IPv6:
        length = size + 40          # 40 = IPv6 固定头
        建 MutableIpv6Packet，设 src/payload_length/version(6)，写入 p
  4. 返回 msg（即裸 IP 报文）
```

注意 IPv4 头长度记的是**整包总长**（`set_total_length`），IPv6 头记的是**载荷长度**（`set_payload_length`，不含 40 字节固定头）。这一差异会在接收端 `inner_length` 还原时体现（u5-l5）。

#### 4.2.3 源码精读

[tests.rs:15-56](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/tests.rs#L15-L56) —— `make_packet` 全貌。IPv4 分支用 `MutableIpv4Packet::minimum_packet_size()`（即 20）算总长，IPv6 分支用 `MutableIpv6Packet::minimum_packet_size()`（即 40）。`src.version` 与 `dst.version` 必须一致，否则 `panic!`。

```rust
let length = size + MutableIpv4Packet::minimum_packet_size();
msg.resize(length, 0);
let mut packet = MutableIpv4Packet::new(&mut msg[..]).unwrap();
packet.set_total_length(length.try_into().expect("length too great for IPv4 packet"));
packet.set_version(4);
```

[Cargo.toml:51-54](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/Cargo.toml#L51-L54) —— `pnet`、`proptest`、`rand_chacha` 都是 `[dev-dependencies]`，只在 `cargo test` 编译，不会进入发布二进制，体现了「测试工具不污染生产产物」的边界。

#### 4.2.4 代码实践（源码阅读型）

1. **目标**：体会「确定性载荷」对测试的意义。
2. **步骤**：阅读 `test_bidirectional` 中 `make_packet(*body_size, src, dst, id as u64)` 的调用（[router/tests/tests.rs:438-443](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/tests/tests.rs#L438-L443)）。
3. **观察现象**：即便 `body_size` 是随机的，因为 `id` 是循环下标（确定），每个包的内容都可复现。
4. **预期结果**：一旦某次测试失败，`id` 能精确定位是哪一个包出问题，便于复现。
5. 运行结果：「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：一个 `make_packet(1000, src_v4, dst_v4, 7)` 产出的报文，裸 IP 包总长是多少？它会被 padding 对齐到多少字节（MTU=1500）？

**参考答案**：IPv4 总长 = 1000 + 20 = 1020 字节。padding 把它向上取整到 16 的倍数：\(\lceil 1020/16\rceil \times 16 = 64 \times 16 = 1024\)，且 \(1024 \le 1500\)，故对齐后 1024 字节（比原包多 4 字节填充）。接收端用 IP 头里的 `total_length=1020` 还原真实长度，剥掉那 4 字节填充。

**练习 2**：为什么用 `ChaCha8Rng` 而不是 `OsRng` 来填载荷？

**参考答案**：测试需要**可复现**。`OsRng` 每次都不同，失败时无法重现；`seed_from_u64(id)` 是确定性的，同一个 `id` 永远产出同一份字节，便于比对与调试。

---

### 4.3 dummy 平台：TunTest / PairBind / UnitEndpoint

#### 4.3.1 概念说明

dummy 平台是「整个协议核心能在 `cargo test` 里跑」的根基（详见 u2-l4）。本节只从**测试视角**补充三点：网卡怎么造、虚拟网线怎么连、端点为何是「空」的。三者的关键设计都是「把系统调用替换成 `std::sync::mpsc` 通道」。

#### 4.3.2 核心流程

- **TunTest**（虚拟网卡）：测试侧 `TunFakeIO` 与 WireGuard 侧 `TunReader`/`TunWriter` 共享一对通道。`TunFakeIO::write` 把包送进 WireGuard（模拟内核→TUN），`TunFakeIO::read` 把 WireGuard 解密后的包取走（模拟 TUN→内核）。
- **PairBind**（虚拟网线）：`pair()` 用两条容量 128 的 `sync_channel` **交叉**连接——实例 1 的 writer 接实例 2 的 reader，反之亦然，于是从一端写出的 UDP 密文会从另一端的 reader 读到。
- **UnitEndpoint**（空端点）：dummy 没有真实 IP/端口，`UnitEndpoint` 是零字段结构，`from_address` 忽略输入、`into_address` 返回占位 `127.0.0.1:8080`、`clear_src` 空操作。这够用了，因为虚拟网线是「直连」，端点信息无关紧要。

#### 4.3.3 源码精读

[dummy.rs:162-192](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/dummy/tun/dummy.rs#L162-L192) —— `TunTest::create(store)`：`store=true` 时两条通道容量都用 32，`false` 时用 1。返回的 `(TunFakeIO, TunReader, TunWriter, TunStatus)` 与 `main.rs` 里 `plt::Tun::create` 返回的三元组语义对应，只是测试多暴露一个 `TunFakeIO` 当「内核端」。

[dummy.rs:86-105](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/dummy/tun/dummy.rs#L86-L105) —— `TunReader::read(buf, offset)`：从通道收一个包，拷进 `buf[offset..]`。注意 `offset` 参数——它就是 u3-l2 讲的「为就地构造传输头预留的前缀」，dummy 忠实实现了这一契约，使协议核心无须知道自己在 dummy 上。

[udp.rs:133-170](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/dummy/udp.rs#L133-L170) —— `PairBind::pair()`：造两条 `sync_channel(128)`，**交叉**分配——`PairWriter{send: tx2}` 配 `PairReader{recv: rx2}`，但 writer1 的 `send` 指向 `tx2`、reader2 的 `recv` 指向 `rx2`，于是「实例 1 写 → 实例 2 读」。容量 128 提供背压。

[udp.rs:191-196](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/dummy/udp.rs#L191-L196) —— 故意把 `PlatformUDP::bind` 实现为「永远返回错误」。测试**不走** UAPI 触发的创建路径，而是直接用 `PairBind::pair()` 手动装配（呼应 u2-l4 的固有边界）。

#### 4.3.4 代码实践（源码阅读型）

1. **目标**：验证「虚拟网线」是双向交叉的。
2. **步骤**：对照 `pair()` 里 `tx1/rx1`、`tx2/rx2` 的分配，画出实例 1 的 `PairWriter.send` 与实例 2 的 `PairReader.recv` 各自指向哪条通道。
3. **观察现象**：实例 1 的 writer 持 `tx2`，实例 2 的 reader 持 `rx2`，二者构成一对接收端在实例 2 的链路；反之实例 2 写、实例 1 读走 `tx1/rx1`。
4. **预期结果**：两条独立的单向链路，互不干扰，正好模拟全双工 UDP。
5. 运行结果：无需运行，纯静态阅读。

#### 4.3.5 小练习与答案

**练习 1**：`PlatformUDP::bind` 在 dummy 上为何返回错误，而测试却仍能跑通？

**参考答案**：dummy 故意让 `bind` 失败，是为了阻止「走 UAPI 自动创建监听器」这条路径——dummy 没有真实端口概念。测试改用 `PairBind::pair()` 手动造好两对 reader/writer，再用 `wg.set_writer`/`wg.add_udp_reader` 装配进去，绕开了 `bind`。

**练习 2**：`TunStatus::event` 首次返回 `Up(1420)` 之后会怎样？

**参考答案**：进入一个 `thread::sleep(60*60s)` 的死循环（[dummy.rs:132-141](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/dummy/tun/dummy.rs#L132-L141)），即「首次 Up 后再不产生新事件」。这与真实 Linux 平台靠 netlink 持续监听 Up/Down 不同——dummy 只需要一次 Up 把设备拉起，_MTU 在测试里由 `wg.up(1500)` 直接指定_。

---

### 4.4 proptest 属性测试：cookie 与共享密钥

#### 4.4.1 概念说明

`proptest` 与普通 `#[test]` 的区别：普通测试写死一两个输入；`proptest` 给参数声明类型（如 `inner : Vec<u8>`），框架会自动生成上百个随机输入，反复调用测试函数。只要**任意一个**输入让断言失败，它就收缩（shrink）出一个最小失败用例报给你。这种测试最擅长捕捉「边界/随机输入」触发的 bug。

wireguard-rs 用它测两个密码学性质：

- **cookie 闭环**（macs.rs）：任意内层报文 + 任意 receiver id，mac1 生成→校验→cookie 回复→处理→mac2 生成→校验，整条链路都应成立。
- **共享密钥唯一性**（device.rs）：任意两把不同的公钥，与同一私钥算出的 DH 共享密钥必不相同。

#### 4.4.2 核心流程

```text
proptest! {
    fn test_cookie_reply(inner1: Vec<u8>, inner2: Vec<u8>, receiver: u32) {
        generator.generate(inner1)  → 应得到非零 mac1、零 mac2
        validator.check_mac1(inner1) → 应通过
        validator.check_mac2(...)    → 应 false（尚无 cookie）
        validator.create_cookie_reply(receiver, src, macs) → 造回复
        generator.process(reply)     → 消费 cookie
        generator.generate(inner2)  → 应得到非零 mac1 与非零 mac2
        validator.check_mac1/mac2(inner2) → 均应通过
    }
}
```

整段是一个「性质」：对任意 `inner1/inner2/receiver`，上述所有断言都成立。proptest 默认跑 256 个随机 case。

#### 4.4.3 源码精读

[macs.rs:295-325](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/macs.rs#L295-L325) —— `test_cookie_reply`。参数 `inner1`/`inner2`/`receiver` 由 proptest 随机生成；先验证「无 cookie 时 mac2 必为零」，再造 cookie 回复、处理，最后验证「拿到 cookie 后 mac2 非零且校验通过」。这条性质正是 u4-l4 抗 DoS 两层 MAC 机制正确性的回归。

[device.rs:487-514](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/device.rs#L487-L514) —— `unique_shared_secrets`：随机 `sk/pk1/pk2`，把两个 peer 加入 device，然后收集所有 peer 的 `ss`（DH 共享密钥）到 `HashSet`，断言「不同 peer 的 ss 唯一」(`ss.len() == dev.len()`)。它顺带覆盖了 u4-l2 的 `add` 去重逻辑：当 `pk1==pk2` 时第二次 `add` 应失败，且原 opaque 值不变。

[handshake/tests.rs:17-50](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/tests.rs#L17-L50) —— `setup_devices` 是握手测试的公用脚手架：用 `OsRng` 生成两对密钥与一个随机 PSK，配好两个互相对应的 `Device<O>`，供 `handshake_no_load`/`handshake_under_load` 复用。

#### 4.4.4 代码实践（源码阅读型）

1. **目标**：体会 proptest 的「收缩」价值。
2. **步骤**：临时把 `test_cookie_reply` 里的某条断言改错（例如把 `assert_ne!(macs.f_mac1, [0u8; SIZE_MAC])` 改成 `assert_eq!`），运行 `cargo test test_cookie_reply`。
3. **观察现象**：proptest 不会只报「某个随机输入失败」，而是不断缩小 `inner1`，最终给出一个最小失败输入（通常是空 `Vec` 或极短字节）。
4. **预期结果**：理解为何属性测试比手写几个固定输入更能暴露边界 bug。改完后务必还原断言。
5. 运行结果：「待本地验证」（注意：本步骤要求临时改测试源码，验证后应还原，属本地探索行为）。

#### 4.4.5 小练习与答案

**练习 1**：`test_cookie_reply` 在生成第一条消息后断言 `macs.f_mac2 == [0u8; SIZE_MAC]`，为什么此时 mac2 必为零？

**参考答案**：mac2 依赖 cookie，而 cookie 要等对端在「过载」时回送 CookieReply 后才存在（u4-l4）。生成第一条消息时尚未处理过任何 cookie，Generator 没有可用的 cookie，故 mac2 只能填零——这也是「非过载时只查 mac1」的体现。

**练习 2**：`unique_shared_secrets` 用 `HashSet<[u8;32]>` 去重，若两个不同公钥碰巧算出相同的 `ss`，这意味着什么？

**参考答案**：理论上 Curve25519 DH 下这几乎不可能发生；若发生，要么是密钥退化（如全零共享密钥，u4-l3 会用 `ct_eq` 拒绝），要么是计算实现错误。这条属性正是把「不同公钥 → 不同会话密钥」这一安全前提变成可自动验证的不变量。

---

### 4.5 Queue 并发模糊测试与路由器回调追踪

#### 4.5.1 概念说明

保序队列 `Queue<J>`（u5-l4）是路由器「并行加密、有序发送」的核心，它的正确性依赖单个原子计数器 `contenders` 实现的无锁互斥。这类并发数据结构最怕竞态——单线程测试测不出，必须**多线程随机压测**才能暴露。

wireguard-rs 提供两条 Queue 测试：

- `test_consume_queue`：带睡眠的「真干活」压测，断言「每个任务恰好执行一次 + 队列最终为空 + 执行数 == 入队数」。
- `test_fuzz_queue`：百万次纯 push/consume 模糊测试，断言「队列最终为空」。

另外，路由器数据面的回归（出站/双向）用 `EventTracker`（基于 `mpsc::channel`）追踪 `Callbacks` 触发的事件，把「异步、多线程」的行为变成「可按序断言」的事件流。

#### 4.5.2 核心流程

```text
test_consume_queue:
  queue = Arc<Queue<TestJob>>;  counter = Arc<AtomicUsize(0)>
  两个线程并发跑 hammer():
      重复 10000 次：随机决定 push 一个 TestJob（内含随机 sleep） 或 consume
  join 后再 consume 一次排空
  断言：queue.len()==0 且 jobs(入队数) == counter(执行数)
```

`TestJob::sequential_work` 里 `thread::sleep` 制造长短不一的临界区，最大化触发「 contender 重入」与「队首未就绪」分支，这正是并发 bug 的高发区。

#### 4.5.3 源码精读

[queue.rs:106-169](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/queue.rs#L106-L169) —— `test_consume_queue`。`hammer` 用 `thread_rng()` 随机决定 push 还是 consume，且 push 的任务带 `wait_sequential`（随机睡眠）。结尾三条断言同时守护三个不变量：**排空、恰好一次、计数一致**。

[queue.rs:172-208](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/queue.rs#L172-L208) —— `test_fuzz_queue`：两线程各做 100 万次随机 push/consume，最后只断言 `queue.len()==0`。它不关心执行次数，只关心「无泄漏、无死锁、最终一致」。

[queue.rs:44-91](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/queue.rs#L44-L91) —— 被测对象 `Queue::consume`：`fetch_add` 抢号、`pos>0` 即返回、临界区内循环处理队首已就绪任务、`fetch_sub` 一次性归还并据此决定重入。模糊测试反复砸的就是这段逻辑。

[router/tests/tests.rs:21-50](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/tests/tests.rs#L21-L50) —— `EventTracker<E>`：把 `Callbacks` 的异步触发（`send`/`recv`/`need_key`/`key_confirmed`）转成可 `wait(timeout)` 的事件流。[router/tests/tests.rs:88-99](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/tests/tests.rs#L88-L99) 的 `no_events!` 宏则断言「此刻不该有任何残留事件」，用于在每个步骤后确认系统已归于平静。

[router/tests/mod.rs:24-48](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/tests/mod.rs#L24-L48) —— `dummy_keypair(initiator)` 造一对固定的假密钥（`0x53`/`0x52` 填充），让路由器测试无须真做 DH 就能跑加解密管道，与 `pad()`（[mod.rs:18-22](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/tests/mod.rs#L18-L22)）一起构成路由器测试的公用件。

> 顺带一提：`router/tests/bench.rs` 里的 `bench_router_outbound` 用 `#[bench]`（仅 `unstable` feature）测吞吐，可选配 `profiler` feature 输出 CPU 剖析（[bench.rs:93-147](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/tests/bench.rs#L93-L147)）。这是「性能」而非「正确性」测试，用 `BencherCallbacks` 累计收发字节数。

#### 4.5.4 代码实践（源码阅读型）

1. **目标**：理解 `consume` 的「重入」为何不会丢任务。
2. **步骤**：对照 [queue.rs:53-90](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/queue.rs#L53-L90)，看 `while contenders > 0` 循环里 `fetch_sub(contenders)` 如何把「在我处理期间新到的 contender」算进下一轮。
3. **观察现象**：即便多个线程同时 `consume`，最终只有一个线程留在临界区排空队列，其余早早 `return`。
4. **预期结果**：`test_consume_queue` 的「jobs == counter」断言在多线程随机睡眠下依然成立，证明无丢任务、无重复执行。
5. 运行结果：「待本地验证」。

#### 4.5.5 小练习与答案

**练习 1**：`test_consume_queue` 为何要在 `TestJob::sequential_work` 里 `thread::sleep`？

**参考答案**：故意拉长临界区持续时间，让「一个线程正在排空时，另一个线程 push 并 consume」的概率大大上升，从而高频触发 `contenders` 的接力与重入分支。没有睡眠的话临界区太短，竞态几乎不会发生，测试也就失去了压测意义。

**练习 2**：`EventTracker::wait` 用带超时的 `recv_timeout`，为什么不用无限阻塞的 `recv`？

**参考答案**：为了**失败时不死等**。如果某个回调本该触发却没触发，`recv` 会让测试挂死；`recv_timeout(TIMEOUT)`（1 秒）到点返回 `None`，断言会立刻失败并给出清晰报错。这是异步测试「快速失败」的常用手法。

---

## 5. 综合实践

> **任务**：基于 `test_pure_wireguard`，编写一个新的端到端测试 `test_pure_wireguard_large_packets`：**只发送报文体积大于 MTU 一半的包**，验证它们经过 padding 对齐后仍能逐字节、按序送达对端。

### 5.1 为什么这个任务有意义

回顾 u3-l2 的 padding 函数（[workers.rs:47-55](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L47-L55)）：

\[ \mathrm{padding}(s, m) = \min\!\left(m,\; s + (16 - s \bmod 16) \bmod 16\right) \]

它对报文长度 `s`（裸 IP 包长度）做两件事：向上取整到 16 的倍数；但绝不超过 MTU `m`。由此产生两个必须验证的风险点：

1. **截断风险**：当 `s > m` 时，`min` 把长度压回 `m`，`tun_worker` 会执行 `msg.truncate(SIZE_MESSAGE_PREFIX + padded)`（[workers.rs:86](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L86)），超长部分被丢弃，包会损坏。所以测试载荷必须保证 `s = 载荷 + IP头 ≤ MTU`。
2. **保序风险**：大报文经过加密管道与保序 `Queue`，若 `Queue` 在大报文（更长的加密耗时）下出错，可能乱序。

「只发大于 MTU/2 的包」恰好把每条报文都推到 padding 的「贴近 MTU」区间（要么补几字节到 16 倍数，要么顶到 MTU 上限），是上述两点最敏感的输入分布。原测试 `make_packet(50 * id)` 最大才 950，并未集中压测这一区间。

### 5.2 操作步骤

1. 复制 `test_pure_wireguard`（[tests.rs:71-200](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/tests.rs#L71-L200)）整段，改名 `test_pure_wireguard_large_packets`。
2. **保持装配代码不变**（建两个 TunTest、`PairBind::pair`、密钥、allowed-ip、endpoint 全部照抄）。
3. 只改发包那一段：把载荷体积从 `50 * id` 改为「严格大于 MTU/2、且总长不超过 MTU」的序列。

示例代码（标注为「示例代码」，需放到 `src/wireguard/tests.rs` 的测试模块内）：

```rust
// 示例代码：仅替换 test_pure_wireguard 中的发包段落
let num_packets = 14;
let mtu = 1500;
let half = mtu / 2; // 750

let mut packets: Vec<Vec<u8>> = Vec::with_capacity(num_packets);
for id in 0..num_packets {
    // 载荷严格大于 MTU/2；最大 id=13 → 1450，IPv4 总长 1470 ≤ MTU 1500，避免被 padding 截断
    let body = 800 + 50 * id as usize; // 800, 850, ..., 1450
    assert!(body > half, "每个包都必须大于 MTU 的一半");
    packets.push(make_packet(
        body,
        "192.168.1.20".parse().unwrap(), // src
        "192.168.2.10".parse().unwrap(), // dst
        id as u64,                       // 确定性 PRNG 种子
    ));
}

let mut backup = packets.clone();
while let Some(p) = packets.pop() {
    fake1.write(p); // 灌进 wg1 网卡 → 触发握手 → 加密 → 经虚拟网线到 wg2
}
while let Some(p) = backup.pop() {
    // 经 padding 对齐 + 加密发出，再解密 + 剥 padding 还原后，应逐字节相等、按序到达
    assert_eq!(
        hex::encode(fake2.read()),
        hex::encode(p),
        "大报文经 padding 对齐后未保持原样或乱序"
    );
}
```

4. 反方向同理可加一段（从 wg2 发往 wg1），与原测试对称。

### 5.3 需要观察的现象与预期结果

- **预期 1**：握手成功，全部 14 个大包都被收到。
- **预期 2**：每个收到的包与发送侧 `backup` 逐字节相同（`hex::encode` 相等），证明 padding 只补齐、未篡改内容。
- **预期 3**：收发顺序一致（两次 `pop` 逆序相互抵消，等价于 FIFO 断言），证明保序 `Queue` 在大报文下仍正确。
- **边界自检**：`assert!(body > half)` 让「大于 MTU 一半」这一前提在测试里被显式断言，而非靠人眼。

运行命令：

```bash
cargo test --release test_pure_wireguard_large_packets -- --nocapture
```

> 运行结果：「待本地验证」。若你把 `body` 调到超过 `MTU - IP头`（例如 `body = 1500`，IPv4 总长 1520 > 1500），应能观察到包被 padding 截断、断言失败——这正是「截断风险」的反证，可作为附加实验。

## 6. 本讲小结

- wireguard-rs 用 **dummy 平台**（`TunTest`/`PairBind`/`UnitEndpoint`）把系统调用替换成内存通道，使整条握手+收发链路可在 `cargo test` 里无 root、无内核模块地端到端跑通，`test_pure_wireguard` 是其核心回归。
- **`make_packet`** 借 `pnet` 构造字段合法、载荷确定性的 IPv4/IPv6 报文；`pnet`/`proptest`/`rand_chacha` 都是 `[dev-dependencies]`，不进发布产物。
- **`proptest`** 把「cookie 闭环」与「共享密钥唯一性」两类密码学性质变成可自动生成海量输入的属性测试，并能收缩出最小失败用例。
- 路由器保序队列 `Queue` 靠 **`test_consume_queue`/`test_fuzz_queue`** 做多线程随机压测，守护「恰好一次、按序、最终排空」三个并发不变量；路由器数据面用 **`EventTracker`** 把异步回调变成可按序断言的事件流。
- 综合实践通过「只发大于 MTU/2 的包」专门压测 padding 对齐的截断与保序风险，验证大报文仍能逐字节、按序送达。

## 7. 下一步学习建议

- **回头串读**：把本讲的 `test_pure_wireguard` 与 u3-l1（胶水层）、u3-l2（padding）、u5-l4（保序 Queue）、u5-l5（cryptokey 路由）对照阅读，你会看到测试里的每一行装配都对应一个已讲过的机制。
- **扩展测试**：尝试基于 `test_bidirectional`（[router/tests/tests.rs:245-482](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/tests/tests.rs#L245-L482)）写一个覆盖 IPv6 大报文双向收发的回归，验证 IPv6 头（`payload_length`，总长 +40）下的 padding 行为。
- **真实平台对照**：阅读 `src/platform/linux/tun.rs`、`src/platform/linux/udp.rs`（u2-l2、u2-l3），理解 dummy 故意省略的真实 IO 行为（netlink Up/Down、sticky socket 漫游），从而清楚 dummy 测试的覆盖边界。
- **性能方向**：在 nightly + `unstable` feature 下运行 `bench_router_outbound`（[bench.rs:93-147](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/tests/bench.rs#L93-L147)），结合 `profiler` feature 输出 CPU 剖析，把「正确性测试」推进到「性能测试」。
