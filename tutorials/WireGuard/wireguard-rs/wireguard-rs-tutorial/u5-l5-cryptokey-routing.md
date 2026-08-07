# Cryptokey 路由表与 IP 解析

## 1. 本讲目标

本讲聚焦 WireGuard 数据面的「导航系统」——cryptokey 路由表。学完后你应该能够：

1. 说清 **cryptokey 路由**的核心思想：出站按**目的地址**选 peer，入站按**源地址**验证 peer。
2. 看懂 `RoutingTable` 如何用 `treebitmap` 的 `IpLookupTable` 实现 IPv4/IPv6 的**最长前缀匹配**。
3. 掌握 `insert / get_route / check_route / list / remove` 五个方法的职责与调用点。
4. 理解 `IPv4Header` / `IPv6Header` 的 `#[repr(packed)]` 布局，以及 `zerocopy::LayoutVerified` 如何做零拷贝解析。
5. 知道 `inner_length` 在入站管道里如何区分数据包与 keepalive、并剥掉尾部 AEAD 标签。

本讲只讲「查表与报文头解析」，不涉及加解密（已在 u5-l2/u5-l3 讲过）与密钥轮转（u5-l6）。

## 2. 前置知识

- **最长前缀匹配（Longest-Prefix Match, LPM）**：路由表里可能有多条前缀同时覆盖同一个地址，例如 `192.168.0.0/16` 和 `192.168.1.0/24` 都覆盖 `192.168.1.20`。LPM 规则是「谁的前缀最长（最具体）就选谁」，所以 `192.168.1.20` 命中 `/24`。记前缀长度为 \(m\)，地址总长为 \(L\)（IPv4 \(L=32\)，IPv6 \(L=128\)），则匹配意味着地址的前 \(m\) 位与表项一致。
- **cryptokey 路由**：WireGuard 不用系统路由表，而是为每个 peer 配置一组 `allowed-ips`（允许的 IP 子网）。这同一张表承担两个方向：发出去的包按**目的地址**查表决定送给哪个 peer；收进来的包按**源地址**查表验证它是否真的来自该 peer。这就是「cryptokey」——路由判定与加密 peer 绑定在一起。
- **`#[repr(packed)]` 与 `zerocopy`**：网络报文按字节顺序紧凑排列，没有 Rust 结构体默认的内存对齐填充。`repr(packed)` 让结构体字段贴着排，`zerocopy::LayoutVerified` 则把一段 `&[u8]`「覆盖」成结构化视图，既不拷贝数据也不违反别名规则。相关背景见 u7-l3。
- **前缀正规化（mask）**：一个 `/24` 网络地址必须把主机位置零，例如 `192.168.1.0/24` 合法，而 `192.168.1.128/24` 的主机位非零。`treebitmap` 要求存入的键已是正规化网络地址。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [`src/wireguard/router/route.rs`](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/route.rs#L1-L139) | `RoutingTable<T>`：IPv4/IPv6 双表、`insert/list/remove/get_route/check_route` |
| [`src/wireguard/router/ip.rs`](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/ip.rs#L1-L49) | `IPv4Header/IPv6Header` 报文头视图、`VERSION_IP4/IP6`、`inner_length` |
| [`src/wireguard/router/device.rs`](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/device.rs#L34-L35) | `table: RoutingTable<Peer<...>>` 字段，`Device::send` 调用 `get_route` |
| [`src/wireguard/router/peer.rs`](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L519-L540) | `add_allowed_ip/list_allowed_ips/remove_allowed_ips` 转调 table |
| [`src/wireguard/router/receive.rs`](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/receive.rs#L107-L113) | 入站并行阶段调用 `check_route`；串行阶段调用 `inner_length` |

依赖关系：`route.rs` `use super::ip::*`，二者是两个叶子模块，只依赖 `treebitmap`、`zerocopy`、`std::net`，不含任何密码学。

---

## 4. 核心概念与源码讲解

### 4.1 Cryptokey 路由的核心思想：出站按目的、入站按源

#### 4.1.1 概念说明

普通路由表只解决「这个包往哪送」（按目的地址转发）。WireGuard 的 cryptokey 表额外解决第二个问题：「收到的这个包，源地址是否真的属于解密它的那个 peer」。这是为了**防伪造**：假设攻击者拿到了 peer B 的会话密钥，它可以用 B 的密钥加密一个内层源地址为 `10.0.0.5`（属于 peer A 的子网）的包。如果没有源地址校验，这个包会被错误地当成来自 A 子网的流量写回 TUN，可能绕过 ACL 或制造路由混乱。

因此同一张 `allowed-ips` 表被用于两个**对称但方向相反**的查询：

- **出站（`get_route`）**：本机产生一个明文 IP 包，按**目的地址**查表，选出负责该目的子网的 peer，把包加密发给它。
- **入站（`check_route`）**：收到并解密一个包后，按**源地址**查表，验证该源子网确实归属当前 peer。

#### 4.1.2 核心流程

```
出站（tun_worker → Device::send）
   明文 IP 包 ──get_route(按目的地址 longest_match)──▶ 选出 peer ──▶ 加密发送

入站（ReceiveJob::parallel_work）
   解密后的明文负载 ──check_route(按源地址 longest_match)──▶ 校验通过？ ─▶ 是：继续 sequential_work
                                                                     └─▶ 否：截断缓冲，丢弃
```

两个方向都用 `longest_match`，区别只在取哪个地址（目的 vs 源）和返回什么（peer vs 布尔）。

#### 4.1.3 源码精读

出站入口在 [`Device::send`](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/device.rs#L181-L201)，它跳过 16 字节传输头前缀后调用 `get_route`：

```rust
let packet = &msg[SIZE_MESSAGE_PREFIX..];
let peer = self.state.table.get_route(packet)
    .ok_or(RouterError::NoCryptoKeyRoute)?;  // 查不到就报错
peer.send(msg, true);
```

入站校验在 [`ReceiveJob::parallel_work`](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/receive.rs#L107-L113)，AEAD 解密通过后再查路由：

```rust
// check crypto-key router
packet.len() == SIZE_TAG || peer.device.table.check_route(&peer, &packet)
```

短路逻辑的含义：**keepalive**（解密后负载恰好等于一个 16 字节 AEAD 标签，即 `packet.len() == SIZE_TAG`）直接放行，因为 keepalive 没有内层 IP 包、无从校验源地址；否则才调用 `check_route`。失败时 `parallel_work` 把缓冲 `truncate(0)`，使后续串行阶段解析报文头失败而提前返回（见 u5-l3）。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：把 `get_route` 与 `check_route` 的调用点画成两张时序图。
2. **步骤**：在 `device.rs` 中找到 `Device::send`，沿 `self.state.table.get_route(...)` 一路看到 `route.rs`；再在 `receive.rs` 中找到 `check_route(...)` 调用，确认它取的是解密后的明文负载。
3. **需要观察的现象**：出站查表发生在**加密之前**（决定送给谁），入站查表发生在**解密之后**（验证源地址）。
4. **预期结果**：两条链路分别落在 `send` 管道和 `receive` 管道，查表方向一「目的」一「源」。
5. 本步无需运行，纯阅读。

#### 4.1.5 小练习与答案

**练习**：为什么 keepalive（`packet.len() == SIZE_TAG`）要短路跳过 `check_route`？

**答案**：keepalive 的内层负载长度为 0，解密后只剩 16 字节 Poly1305 标签，既没有合法的 IP 版本字段也没有源地址，强行 `check_route` 一定返回 false。所以用短路把它排除在路由校验之外，让 keepalive 能正常完成密钥确认（见 u5-l3 的密钥静默确认机制）。

---

### 4.2 IP 报文头的零拷贝解析（ip.rs）

#### 4.2.1 概念说明

`get_route` / `check_route` / `inner_length` 都要从一段原始字节里取出「IP 版本」「源地址」「目的地址」「总长度」。wireguard-rs 不把字节先反序列化进一个临时结构体，而是用 `zerocopy::LayoutVerified` 直接把 `&[u8]` **就地**当成 `IPv4Header` / `IPv6Header` 视图来读写。这样在数据面热路径上零拷贝、零分配。

要做到这一点，结构体必须满足两个条件：字段布局与线路字节**一一对应**（`#[repr(packed)]`），且可被安全地当作字节解释（`FromBytes` / `AsBytes`）。

#### 4.2.2 核心流程

1. 取首字节的高 4 位（`packet[0] >> 4`）判断 IP 版本：`4` → IPv4，`6` → IPv6。
2. 用 `LayoutVerified::new_from_prefix(packet)` 校验缓冲**至少**容纳一个报文头，返回 `(头视图, 剩余字节)`。
3. 通过视图的 `f_source` / `f_destination` 字段直接读地址，通过 `f_total_len` / `f_len` 读长度。

`IPv4Header` 字段在缓冲里的字节偏移如下（共 20 字节，与 RFC 791 对应）：

| 结构体字段 | 字节偏移 | 含义 |
|-----------|---------|------|
| `_f_space1: [u8; 2]` | 0–1 | version/IHL、DSCP/ECN（路由不关心，下划线前缀表示忽略） |
| `f_total_len: U16<BigEndian>` | 2–3 | 整个 IP 包总长度（头+负载） |
| `_f_space2: [u8; 8]` | 4–11 | 标识、标志/分片、TTL、协议、头校验和 |
| `f_source: [u8; 4]` | 12–15 | 源地址 |
| `f_destination: [u8; 4]` | 16–19 | 目的地址 |

`IPv6Header` 共 40 字节：`_f_space1[4]`（version/流标签）、`f_len: U16<BigEndian>`（**负载**长度，不含 40 字节固定头）、`_f_space2[2]`（next header/hop limit）、`f_source[16]`、`f_destination[16]`。

#### 4.2.3 源码精读

版本常量与两个头结构定义在 [ip.rs:8-29](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/ip.rs#L8-L29)：

```rust
pub const VERSION_IP4: u8 = 4;
pub const VERSION_IP6: u8 = 6;

#[repr(packed)]
#[derive(Copy, Clone, FromBytes, AsBytes)]
pub struct IPv4Header {
    _f_space1: [u8; 2],
    pub f_total_len: U16<BigEndian>,
    _f_space2: [u8; 8],
    pub f_source: [u8; 4],
    pub f_destination: [u8; 4],
}
```

> 说明：`#[repr(packed)]` 让字段紧凑排列、与线路字节对齐；`U16<BigEndian>` 是 zerocopy 的字节序包装类型，`.get()` 内部用 `read_unaligned` 安全读取 packed 字段（避免因对齐产生 UB）。带下划线前缀的 `_f_space1/_f_space2` 是路由逻辑不关心但仍需占位的字段。

[`inner_length`](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/ip.rs#L31-L49) 取「真实 IP 包长度」：

```rust
pub fn inner_length(packet: &[u8]) -> Option<usize> {
    match packet.get(0)? >> 4 {
        VERSION_IP4 => {
            let (header, _) = LayoutVerified::new_from_prefix(packet)?;
            Some(header.f_total_len.get() as usize)        // IPv4: 头+负载总长
        }
        VERSION_IP6 => {
            let (header, _) = LayoutVerified::new_from_prefix(packet)?;
            Some(header.f_len.get() as usize + mem::size_of::<IPv6Header>()) // IPv6: 负载 + 40
        }
        _ => None,
    }
}
```

> 说明：IPv4 的 `f_total_len` 本身就是整个包长；IPv6 的 `f_len` 只是负载长度，必须加回 40 字节固定头。注意两个 arm 都先 `new_from_prefix`，所以**过短的缓冲**（如 keepalive）会返回 `None`。

`inner_length` 在 [`receive.rs:174`](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/receive.rs#L174-L180) 被用来决定写多少字节进 TUN：

```rust
if let Some(inner) = inner_length(packet) {
    if inner + SIZE_TAG <= packet.len() {
        let _ = peer.device.inbound.write(&packet[..inner]) ...
    }
}
```

> 说明：`packet` 此时是「明文 IP 包 + 16 字节 AEAD 标签」。`inner_length` 给出明文 IP 包的真实长度 `inner`，`&packet[..inner]` 正好剥掉尾部标签；`inner + SIZE_TAG <= packet.len()` 是一致性校验。返回 `None`（keepalive/畸形）则不写 TUN。

#### 4.2.4 代码实践（断言型）

1. **目标**：用编译期/运行期断言验证两个头结构的字节数。
2. **步骤**：在 `ip.rs` 的 `#[cfg(test)]` 里加：
   ```rust
   #[test]
   fn header_sizes() {
       assert_eq!(core::mem::size_of::<IPv4Header>(), 20);
       assert_eq!(core::mem::size_of::<IPv6Header>(), 40);
   }
   ```
3. **需要观察的现象**：编译能通过（`repr(packed)` 无填充），断言成立。
4. **预期结果**：IPv4=20，IPv6=40。
5. 推导：\(2+2+8+4+4=20\)，\(4+2+2+16+16=40\)。

#### 4.2.5 小练习与答案

**练习**：为什么 IPv6 的 `inner_length` 要 `+ mem::size_of::<IPv6Header>()`，而 IPv4 不用加？

**答案**：IPv4 头里的 `f_total_len` 字段语义是「整个包（含头）」的长度，已包含 20 字节头；IPv6 头里的 `f_len` 字段语义是「负载长度」，**不含** 40 字节固定头，所以必须手动加回 `size_of::<IPv6Header>()`（=40）才得到完整包长。

---

### 4.3 RoutingTable 数据结构与维护（route.rs + treebitmap）

#### 4.3.1 概念说明

`RoutingTable<T>` 是 cryptokey 路由的存储核心。它内部用 [`treebitmap`](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/Cargo.toml#L33-L35) crate（实际包名 `ip_network_table-deps-treebitmap`，0.5.0）提供的 `IpLookupTable<A, T>`——一棵专为最长前缀匹配优化的压缩基数树（multi-bit trie），查表复杂度接近常数。因为 IPv4 与 IPv6 地址长度不同，wireguard-rs 为两者各维护一张表。

`T` 是「每个子网映射到的值」。在本项目里 `T = Peer<E,C,T,B>`（一个 `Arc` 引用计数的 peer 句柄，`Clone` 廉价）。由于 `RoutingTable` 是泛型的（`T: Eq + Clone`），它不依赖任何 WireGuard 类型，可独立测试（见综合实践）。

#### 4.3.2 核心流程

- **建表**：`new()` 各建一张空的 `IpLookupTable`。
- **插入**：`insert(ip, cidr, value)` 先把 `ip` 用 `mask(cidr)` **正规化**（清主机位），再存入对应表。注意：`treebitmap` 的 `Address::mask` 来自 `use treebitmap::address::Address`，为 `Ipv4Addr`/`Ipv6Addr` 扩展了 `mask` 方法。
- **枚举**：`list(value)` 遍历两张表，收集所有「值等于 `value`」的 `(ip, cidr)`——即某 peer 名下全部 allowed-ips。
- **删除**：`remove(value)` 同样遍历，把属于 `value` 的条目全部删掉。

`list` / `remove` 的私有辅助 [`collect`](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/route.rs#L27-L38) 用 `IpLookupTable::iter()` 线性扫描整表，所以这两个操作是 O(条目数) 的。`insert` 与 `get_route` 则是高效的树操作。

#### 4.3.3 源码精读

表结构定义在 [route.rs:13-24](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/route.rs#L13-L24)：

```rust
pub struct RoutingTable<T: Eq + Clone> {
    ipv4: RwLock<IpLookupTable<Ipv4Addr, T>>,
    ipv6: RwLock<IpLookupTable<Ipv6Addr, T>>,
}
```

> 说明：两张表各包一层 `spin::RwLock`（自旋读写锁，适合临界区极短的场景）。读多写少——查表（`get_route`/`check_route`）走读锁，配置变更（`insert`/`remove`）走写锁。

[`insert`](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/route.rs#L40-L45) 按 IP 版本分流并 `mask`：

```rust
pub fn insert(&self, ip: IpAddr, cidr: u32, value: T) {
    match ip {
        IpAddr::V4(v4) => self.ipv4.write().insert(v4.mask(cidr), cidr, value),
        IpAddr::V6(v6) => self.ipv6.write().insert(v6.mask(cidr), cidr, value),
    };
}
```

> 说明：`v4.mask(cidr)` 清掉主机位后再插入，保证键是正规网络地址。这其实使得 `peer.rs` 里 [`add_allowed_ip`](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L519-L524) 的文档注释（「`ip` 不能有主机位」）成为一条**已被自动满足**的前件——即便调用方误传 `192.168.1.128/24`，`mask` 也会把它正规化成 `192.168.1.0/24`。

调用链：配置层 `add_allowed_ip(ip, masklen)` → `table.insert(ip, masklen, self.peer.clone())`；`list_allowed_ips()` → `table.list(&self.peer)`；`remove_allowed_ips()` → `table.remove(&self.peer)`（见 [peer.rs:519-540](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L519-L540)）。表在 [`DeviceHandle::new`](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/device.rs#L108-L115) 里用 `RoutingTable::new()` 创建。

#### 4.3.4 代码实践（跟踪型）

1. **目标**：跟踪一条 UAPI `allowed_ip` 是如何最终落到 `IpLookupTable` 里的。
2. **步骤**：从 `configuration/uapi/set.rs` 解析到 `allowed_ip=192.168.1.0/24` → 调用 `PeerHandle::add_allowed_ip`（[peer.rs:519](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/peer.rs#L519-L524)）→ `RoutingTable::insert`（[route.rs:40](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/route.rs#L40-L45)）→ `v4.mask(24)` → `IpLookupTable::insert`。
3. **需要观察的现象**：插入前发生了 `mask` 正规化，值是 peer 的克隆（`Arc` 增计数）。
4. **预期结果**：UAPI 配置 `replace_allowed_ips=true` 会先 `remove_allowed_ips`（`table.remove`）再逐条 `insert`，实现全量替换。
5. 本步为阅读型，无需运行。

#### 4.3.5 小练习与答案

**练习**：`list` 与 `remove` 为什么用 `IpLookupTable::iter()` 全表扫描，而不是按 key 直接查？

**答案**：这两个操作的输入是「值」（一个 peer 句柄），而非「键」（ip/cidr）。`IpLookupTable` 的索引是按 ip 前缀组织的，没有「值 → 键」的反向索引，所以只能遍历整张表、用 `==` 比较值来找出该 peer 名下的所有子网。由于 allowed-ips 数量通常很小，O(n) 扫描可以接受。

---

### 4.4 路由查表的两个方向：get_route 与 check_route

#### 4.4.1 概念说明

`RoutingTable` 的两个查表方法分别服务于出站和入站：

- **`get_route(&self, packet) -> Option<T>`**：出站。解析报文头的**目的地址**，做最长前缀匹配，返回命中的 peer（用于后续加密发送）。
- **`check_route(&self, peer, packet) -> bool`**：入站。解析报文头的**源地址**，做最长前缀匹配，返回是否校验通过。

二者都标了 `#[inline(always)]`，因为它们处在每包都走的热路径上。

#### 4.4.2 核心流程

`get_route`（[route.rs:74-114](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/route.rs#L74-L114)）：

```
取 packet[0]>>4 判版本
 ├─ 4 → 解析 IPv4Header → longest_match(f_destination) → Some(peer) / None
 ├─ 6 → 解析 IPv6Header → longest_match(f_destination) → Some(peer) / None
 └─ 其它 → None（非法版本）
```

`check_route`（[route.rs:116-138](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/route.rs#L116-L138)）结构对称，但取 `f_source` 且返回 `bool`。

#### 4.4.3 源码精读

`get_route` 的 IPv4 分支：

```rust
VERSION_IP4 => {
    let (header, _): (LayoutVerified<&[u8], IPv4Header>, _) =
        LayoutVerified::new_from_prefix(packet)?;          // 过短 → None
    self.ipv4.read()
        .longest_match(Ipv4Addr::from(header.f_destination))
        .map(|(_, _, p)| p.clone())                        // (ip,cidr,&T) → T
}
```

> 说明：`longest_match` 返回 `Option<(地址, 前缀长, &T)>`，`.map(|(_, _, p)| p.clone())` 取出 peer。注意 `f_destination` 是 `[u8;4]`，用 `Ipv4Addr::from` 转成标准库地址再查表。

`check_route` 的 IPv4 分支（**请仔细读结尾**）：

```rust
Some(VERSION_IP4) => LayoutVerified::new_from_prefix(packet)
    .and_then(|(header, _): (LayoutVerified<&[u8], IPv4Header>, _)| {
        self.ipv4.read()
            .longest_match(Ipv4Addr::from(header.f_source))
            .map(|(_, _, p)| p == peer)        // Option<(..)> → Option<bool>
    })
    .is_some(),                                  // ← 关键：对 Option<bool> 取 is_some
```

> **本段需要你重点理解**：这里把 `longest_match` 的结果先 `.map(|(_, _, p)| p == peer)` 变成 `Option<bool>`，再 `.is_some()`。由于 `Option<bool>::is_some()` 对 `Some(true)` 和 `Some(false)` **都返回 true**，只有 `None`（源地址在表中无任何匹配）才返回 false。因此 `check_route` 的**实际返回语义**是：「该源地址是否命中表中任意一条 allowed-ip」，而 `p == peer` 的比较结果被 `.is_some()` 折叠掉了。
>
> 把这一行为列成真值表：

| 源地址查表结果 | `p == peer` 的值 | `.map` 产物 | `check_route` 返回 |
|---------------|-----------------|------------|-------------------|
| 命中、属本 peer | `true`  | `Some(true)`  | `true` |
| 命中、属**他** peer | `false` | `Some(false)` | **`true`** |
| 未命中（无匹配前缀） | ——     | `None`        | `false` |

> 也就是说，按当前 HEAD 的代码，只要源地址匹配表里**任意** peer 的子网，`check_route` 就通过——它并未严格校验「源地址必须属于当前这个 `peer`」。一个严格的逐 peer 归属校验本应写成 `.map_or(false, \|(_, _, p)\| p == peer)`（即 `Some(true)` 才放行，`Some(false)` 与 `None` 都拒绝）。这一点与 4.1 描述的「按源地址验证归属」的**设计意图**存在差距，值得在本地用测试确认其行为与影响（见综合实践），后续版本可能修正。

#### 4.4.4 代码实践（分析型）

1. **目标**：亲手验证 `check_route` 在「源地址属于另一个 peer」时的返回值。
2. **步骤**：基于综合实践的 `RoutingTable<u8>` 测试，额外插入：peer `1` 拥有 `10.0.0.0/24`，peer `2` 拥有 `192.168.0.0/24`；构造一个源地址为 `10.0.0.5` 的 20 字节 IPv4 头，调用 `check_route(&2, &packet)`。
3. **需要观察的现象**：源地址 `10.0.0.5` 命中 peer `1`，但传入的 `peer=2`。
4. **预期结果**：`待本地验证`——据 4.4.3 的真值表，返回值应为 `true`（`Some(false).is_some()`）。若你认为应拒绝，请对比 `.map_or(false, ...)` 写法。
5. 提示：构造 20 字节头时，`buf[0]=0x45`（版本 4）、`buf[12..16]` 填源地址即可，`check_route` 只读首字节版本与 `f_source`。

#### 4.4.5 小练习与答案

**练习**：`get_route` 与 `check_route` 都用 `longest_match`，为什么 `get_route` 返回 `Option<T>` 而 `check_route` 返回 `bool`？

**答案**：`get_route` 服务于出站，需要**知道是哪个 peer** 才能把包交给它加密发送，故返回命中的 peer（`None` 表示无路由，`Device::send` 据此报 `NoCryptoKeyRoute`）。`check_route` 服务于入站校验，调用方已经持有 peer（解密它的那个 peer），只需要一个「放行/拒绝」的布尔结论，故返回 `bool`。

---

## 5. 综合实践

**任务**：编写一个单元测试，验证 `RoutingTable` 的最长前缀匹配——向表中插入两条**重叠前缀**，分别属于不同的「peer」，断言 `get_route` 对落在两个前缀交集与只被宽前缀覆盖的地址分别返回正确的 peer。

由于 `RoutingTable` 是 `T: Eq + Clone` 的泛型结构，测试可直接用 `u8` 当 peer，无需构造真实的 `Peer` 对象或 TUN/UDP。`get_route` 接收的是**原始 IP 包字节**（`&[u8]`），所以测试要构造一个最小 IPv4 头（20 字节），只需正确设置首字节版本与目的地址字段即可——`get_route` 只读这两处。

**测试代码**（建议放在 `src/wireguard/router/tests/tests.rs`，引用路径 `super::super::route::RoutingTable`；由于 `route` 是 `router` 的私有子模块、而 `tests` 是 `router` 的后代模块，故可访问。若放置位置不同需相应调整导入路径）：

```rust
use super::super::route::RoutingTable; // 路径取决于测试文件所在位置
use std::net::{IpAddr, Ipv4Addr};

/// 构造一个最小 20 字节 IPv4 头：byte0=0x45(版本4/IHL5)，目的地址填 dst。
/// get_route 只读首字节版本与 f_destination，其余字段可为 0。
fn ipv4_header(dst: Ipv4Addr) -> Vec<u8> {
    let mut buf = vec![0u8; 20];
    buf[0] = 0x45; // version=4, IHL=5
    buf[16..20].copy_from_slice(&dst.octets()); // f_destination 位于字节 16..20
    buf
}

#[test]
fn test_longest_prefix_match() {
    let table = RoutingTable::<u8>::new();

    // 重叠前缀：/16 属 peer 1，更具体的 /24 属 peer 2
    let broad: IpAddr = "192.168.0.0".parse().unwrap();
    let narrow: IpAddr = "192.168.1.0".parse().unwrap();
    table.insert(broad, 16, 1);
    table.insert(narrow, 24, 2);

    // 落在 /24 与 /16 的交集 → 最长匹配应选 peer 2
    let in_both = ipv4_header("192.168.1.20".parse().unwrap());
    assert_eq!(table.get_route(&in_both), Some(2));

    // 只被 /16 覆盖（192.168.2.x 不在 192.168.1.0/24 内）→ 选 peer 1
    let only_broad = ipv4_header("192.168.2.5".parse().unwrap());
    assert_eq!(table.get_route(&only_broad), Some(1));

    // 不被任何前缀覆盖 → None
    let nowhere = ipv4_header("10.0.0.1".parse().unwrap());
    assert_eq!(table.get_route(&nowhere), None);
}
```

**操作步骤**：
1. 把测试加入 `src/wireguard/router/tests/tests.rs`（或 `tests/mod.rs`），修正 `RoutingTable` 的导入路径后 `cargo test --lib test_longest_prefix_match`。
2. 运行并观察三个断言。
3. 扩展：再加一组 IPv6 重叠前缀（如 `2001:db8::/32` 与 `2001:db8:1::/48`），构造 40 字节 IPv6 头验证 `get_route` 的 v6 分支。

**预期结果**：三条断言全部通过，证明 `IpLookupTable::longest_match` 选出的是最具体的前缀。若导入路径报 `private` 错误，把测试放在 `router` 模块的任意后代下即可（私有模块对后代可见）。

> 注意：本测试只覆盖 `get_route`（出站、按目的地址）。`check_route`（入站、按源地址）的边界行为请结合 4.4.4 的实践单独验证。

## 6. 本讲小结

- cryptokey 路由用**同一张** `allowed-ips` 表服务两个方向：出站 `get_route` 按**目的地址**选 peer，入站 `check_route` 按**源地址**校验。
- `RoutingTable<T>` 内部是两张 `treebitmap::IpLookupTable`（v4/v6 各一），用 `spin::RwLock` 保护；`insert` 会先 `mask(cidr)` 正规化网络地址。
- 最长前缀匹配由 `IpLookupTable::longest_match` 提供，`get_route` 据目的地址返回 `Option<peer>`，无路由则 `Device::send` 报 `NoCryptoKeyRoute`。
- `IPv4Header`/`IPv6Header` 用 `#[repr(packed)]` + `zerocopy::LayoutVerified` 做零拷贝解析；版本取 `packet[0]>>4`（`VERSION_IP4=4`、`VERSION_IP6=6`）。
- `inner_length` 给出真实 IP 包长度（IPv4 用 `f_total_len`，IPv6 用 `f_len+40`），用于剥掉尾部 AEAD 标签写回 TUN，并以此识别 keepalive（返回 `None`）。
- 重点：当前 HEAD 的 `check_route` 以 `.map(|(_,_,p)| p==peer).is_some()` 结尾，其**实际**语义是「源地址命中表中任意前缀即放行」，与严格的逐 peer 归属校验存在差距，值得用测试确认。

## 7. 下一步学习建议

- 本讲解释了「查表」，但「密钥如何注入、出站无密钥时如何暂存」属于 peer 生命周期：继续学 **u5-l6（KeyWheel 与 Peer 生命周期）**，看 `add_allowed_ip` 之外的 `add_keypair/confirm_key/staged_packets` 如何与路由表配合。
- 入站侧的顺序敏感操作（防回放 `AntiReplay::update`、密钥确认）在 u5-l3 已讲，与之配套的位图算法见 **u5-l7（防回放窗口 RFC 6479）**。
- 想进一步理解 `LayoutVerified` 在全项目的贯穿用法（含 send/receive 的就地构造），见 **u7-l3（零拷贝报文解析）**。
- 建议动手把综合实践的测试跑通后，再补一个针对 `check_route` 的测试，把 4.4 的真值表落成可执行断言，加深对当前实现行为的理解。
