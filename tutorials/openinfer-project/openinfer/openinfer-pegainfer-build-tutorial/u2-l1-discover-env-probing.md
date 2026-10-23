# discover：环境变量驱动的探测入口

> 适用对象：已读完 u1-l1（项目定位与工作区全景）、并自己通读过一遍 `pegainfer-build/src/lib.rs` 骨架的读者。
> 本讲是单元二「核心探测机制源码精读」的第一讲（u2-l1），聚焦全库最常被调用的 14 行代码：`CudaToolkit::discover()`。
> 所有行号基于 HEAD 提交 `223206b7`；`pegainfer-build/src/lib.rs` 与 `pegainfer-cupti/build.rs` 的行号均在本讲撰写环境中逐行核对。

## 1. 本讲目标

学完本讲，你应当能够：

1. **说清优先级回落链**：`$CUDA_HOME` → `$CUDA_PATH` → `/usr/local/cuda` 的确切语义——尤其是「CUDA_HOME 已设置但无效时，CUDA_PATH 还会被使用吗」这类极易答错的边界。
2. **理解容错设计**：环境变量指向的根不是目录时，discover 只发一条 `cargo:warning` 而不失败；能解释「告警之后无效根仍然被使用」的下游后果（空壳工具包、nvcc 回落 `$PATH`），以及它与 `find_package` 找不到就 panic 的风格差异为什么各自成立。
3. **掌握 rerun-if-env-changed 的语义**：Cargo 以「环境变量的值」为指纹决定是否重跑 build.rs，值不变就不重跑；理解这两条指令为什么由库函数 discover() 自己发射、而不是留给消费方声明。

## 2. 前置知识

### 2.1 discover 在整条链路中的位置（承接 u1-l1）

u1-l1 已确立：pegainfer-build 是构建期专用库，全仓恰有两个消费方（`pegainfer-kernels` 与 `pegainfer-cupti`），都在各自 build.rs 的开头调用 `CudaToolkit::discover()`。本讲只解剖这个入口函数本身；它负责「读环境变量、定根、体检」，然后把剩下的一切交给私有的 `from_root()`（三种安装布局的适配，留给 u2-l3 精读）。

### 2.2 三个「根」的来历

- **`CUDA_HOME`**：老牌惯例，Makefile 时代的 CUDA 项目和许多 ML 框架构建脚本用它指定 CUDA 安装根。
- **`CUDA_PATH`**：NVIDIA 官方安装器（尤其 Windows）写入的变量。两类变量长期并存，工具只好都认。
- **`/usr/local/cuda`**：Linux 上 CUDA 安装器创建的惯例符号链接，通常指向 `/usr/local/cuda-<版本>`，是「什么都没配置」时的社区默认值。

这三条是行业惯例背景；本项目源码只做了选择——先 HOME 后 PATH，最后落 `/usr/local/cuda`。

### 2.3 读这段代码需要的几个 Rust 惯用法

- `Result::or_else(|_| ...)`：前一步返回 `Err` 时改用备用计算。注意 `env::var` 在变量**未设置**、或值**不是合法 UTF-8** 时都返回 `Err`。
- `Result::ok()`：把 `Result` 折叠成 `Option`，错误统一视为「无值」。
- `Option::as_deref()` 与 `Option::filter(...)`：前者把 `Option<String>` 变 `Option<&str>`；后者只在「有值且谓词为真」时保留。L82 用它们一步筛出「设置了环境变量、但它不是目录」的情形。
- `Option::map_or_else(默认分支, Some分支)`：`None` 走默认分支，`Some(v)` 走第二分支；两个分支都直接传函数名 `PathBuf::from`（函数指针惯用法）。

### 2.4 Cargo 为什么不会每次都跑 build.rs

Cargo 为每个 build.rs 维护一份**指纹**（fingerprint）：指纹没变，就复用上次脚本输出的构建指令，脚本整个跳过。指纹的输入包括：脚本自身（或其 build-dependencies）是否重新编译、`cargo:rerun-if-changed` 声明的路径、`cargo:rerun-if-env-changed` 声明的变量的值。关键规则：**一旦 build.rs 发射过任何 rerun-if 指令，Cargo 就放弃默认指纹**（默认指纹约等于「包内任何文件变化就重跑」），只认声明的这些原因。本讲 §4.3 依赖这条规则；缓存正确性的完整风险分析在 u3-l3 展开。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲怎么用 |
| --- | --- | --- |
| [pegainfer-build/src/lib.rs](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs) | 本 crate 唯一源码文件（299 行） | §4.1–§4.3 逐行精读 [L76-L89](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L76-L89) 的 `discover()`；引用 L12-L52、L91-L121、L132-L159 作对照 |
| [pegainfer-cupti/build.rs](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-cupti/build.rs) | 34 行的最小消费方 | §4.3 观察 rerun-if 指令在真实 build.rs 里的聚合方式 |

> 行号可信度：两份文件的行号均在本讲撰写环境中直接读取核对，非抓取推断。

## 4. 核心概念与源码讲解

先把主角请出场——`discover()` 全文只有 14 行：

[pegainfer-build/src/lib.rs:L76-L89](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L76-L89) —— `impl CudaToolkit` 块中唯一的公开构造函数：先声明缓存依赖，再读两个环境变量定出工具包根，体检告警后交给 `from_root`：

```rust
pub fn discover() -> Self {
    println!("cargo:rerun-if-env-changed=CUDA_HOME");
    println!("cargo:rerun-if-env-changed=CUDA_PATH");
    let env_root = env::var("CUDA_HOME")
        .or_else(|_| env::var("CUDA_PATH"))
        .ok();
    if let Some(root) = env_root.as_deref().filter(|root| !Path::new(root).is_dir()) {
        println!(
            "cargo:warning=CUDA root {root} (from CUDA_HOME/CUDA_PATH) is not a directory"
        );
    }
    let root = env_root.map_or_else(|| PathBuf::from("/usr/local/cuda"), PathBuf::from);
    Self::from_root(root)
}
```

下面按三个最小模块拆开讲。

### 4.1 环境变量优先级链与默认根

#### 4.1.1 概念说明

「CUDA 装在哪」在每台机器上答案都不同：系统装在 `/usr/local/cuda-12.6`、conda 环境装在 `$CONDA_PREFIX`、HPC SDK 装在家目录深处。构建脚本不可能内置所有答案，业界通行做法是**环境变量驱动**：用户（或安装器）设一个变量指路，脚本读它。

问题是有两个约定俗成的变量（`CUDA_HOME` 与 `CUDA_PATH`）和一个默认根（`/usr/local/cuda`），于是「优先级」成了必须一锤定音的决策。discover 的答案是一个**回落链**：`CUDA_HOME` 成功读到就用它；读不到（未设置或非 UTF-8）才读 `CUDA_PATH`；两个都读不到才用 `/usr/local/cuda`。

这里有个最容易答错的边界，先划重点：**回落发生在「读取」层面，不在「有效性」层面**。`CUDA_HOME` 只要有值——哪怕指向不存在的目录——就锁定了结果，`CUDA_PATH` 根本不会被看一眼。

#### 4.1.2 核心流程

```
CudaToolkit::discover()                          lib.rs L76
  │
  ├─ ① 缓存契约（L77-L78）：声明 CUDA_HOME / CUDA_PATH 的值变化会触发重跑
  │
  ├─ ② 读环境变量（L79-L81）：
  │      env::var("CUDA_HOME") ──Err──▶ env::var("CUDA_PATH") ──Err──▶ None
  │            │Ok（有值就用，不体检）           │Ok
  │            ▼                                  ▼
  │        Some(CUDA_HOME 的值)           Some(CUDA_PATH 的值)
  │
  ├─ ③ 体检（L82-L86）：Some 且 !is_dir ──▶ cargo:warning（只告警，不改变取值）
  │
  ├─ ④ 定根（L87）：None ──▶ /usr/local/cuda；Some(v) ──▶ v（即使 v 刚被告警）
  │
  └─ ⑤ 交接（L88）：from_root(root) 按三种布局组装目录（u2-l3 的主题）
```

把 ②④ 两步合起来，就是下面这张**决策真值表**（L83 告警列在 §4.2 解释）：

| 场景 | CUDA_HOME | CUDA_PATH | 最终 root | L83 告警？ |
| --- | --- | --- | --- | --- |
| 1 | 未设置 | 未设置 | `/usr/local/cuda` | 否（即使它不存在） |
| 2 | `/home/me/cuda`（存在） | 任意值（**被忽略**） | `/home/me/cuda` | 否 |
| 3 | `/bad/path`（不存在） | `/home/me/cuda`（存在） | `/bad/path` | 是 |
| 4 | 未设置 | `/home/me/cuda` | `/home/me/cuda` | 否 |
| 5 | 未设置 | `/bad/path` | `/bad/path` | 是 |
| 6 | `""`（空字符串） | `/home/me/cuda` | `""` | 是（空串不是目录） |

场景 3 就是「读取层面回落」的直接后果：坏的 `CUDA_HOME` 遮蔽好的 `CUDA_PATH`。

#### 4.1.3 源码精读

**(a) 优先级链本体。**
[pegainfer-build/src/lib.rs:L79-L81](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L79-L81) —— `Result` 组合子三连，先 `CUDA_HOME` 后 `CUDA_PATH`：

```rust
let env_root = env::var("CUDA_HOME")
    .or_else(|_| env::var("CUDA_PATH"))
    .ok();
```

三个观察点：

- `or_else` 只在 `env::var("CUDA_HOME")` 返回 `Err` 时才执行闭包。而 `env::var` 的 `Err` 有两种：变量未设置（`NotPresent`）与值非 UTF-8（`NotUnicode`）——后者意味着「值含非 UTF-8 字节的 CUDA_HOME 会被当作没设置」，悄悄落到 `CUDA_PATH`。对比 [L20](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L20) 的 `find_package` 用的是无损的 `env::var_os`，discover 用 `env::var` 是一个有损的小取舍。
- 变量值为空字符串时 `env::var` 返回 `Ok("")`——所以场景 6 里空串**算设置了**，还会触发告警。
- `.ok()` 把两种 `Err` 统一折叠成 `None`，后续代码只需面对一个 `Option`。

**(b) 默认根与函数指针。**
[pegainfer-build/src/lib.rs:L87](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L87) —— `None` 补默认根，`Some(v)` 原样转 `PathBuf`：

```rust
let root = env_root.map_or_else(|| PathBuf::from("/usr/local/cuda"), PathBuf::from);
```

`map_or_else` 的两个分支都直接传 `PathBuf::from` 这个函数名（第二分支靠类型推断从 `String` 构造），没有闭包包装，是 Rust 里「函数即值」的典型写法。

**(c) 唯一公开入口。**
[pegainfer-build/src/lib.rs:L88](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L88) 与 [L91](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L91) —— `discover` 是 `pub`，`from_root` 没有 `pub`：消费方拿不到 `from_root`，**「环境变量怎么定根」这条策略因此只存在于一处**。想改回落顺序（比如让 `CUDA_PATH` 优先）只动 L79-L81 一行即可，全工作区自动生效。

#### 4.1.4 代码实践：搭一个 scratch 实验室，亲眼看 root 怎么变

本讲后面的实践全部复用这个实验室，一次搭好。

1. **实践目标**：用可运行的证据确认真值表的 2、4、1 三行（HOME 赢、PATH 兜底、默认根）。
2. **操作步骤**：

   在仓库**外**新建目录（放在仓库内会被 workspace 吸收，Cargo 会报 workspace 归属错误）：

   ```
   /tmp/cuda-scratch/
   ├── Cargo.toml
   ├── build.rs
   └── src/main.rs
   ```

   三个文件如下（**示例代码**，非项目原有代码）：

   ```toml
   # /tmp/cuda-scratch/Cargo.toml —— 示例代码
   [package]
   name = "cuda-scratch"
   version = "0.0.0"
   edition = "2021"
   publish = false

   [build-dependencies]
   # 把路径换成你机器上 openinfer 仓库的绝对路径
   pegainfer-build = { path = "/path/to/openinfer/pegainfer-build" }
   ```

   ```rust
   // /tmp/cuda-scratch/build.rs —— 示例代码
   fn main() {
       let toolkit = pegainfer_build::CudaToolkit::discover();
       let secs = std::time::SystemTime::now()
           .duration_since(std::time::UNIX_EPOCH)
           .map(|d| d.as_secs())
           .unwrap_or(0);
       // 时间戳是 §4.3 的探针：build.rs 每次真跑都会打出新的秒数
       println!(
           "cargo:warning=[discover-lab] build.rs 已运行 epoch={secs}s root={}",
           toolkit.root.display()
       );
   }
   ```

   ```rust
   // /tmp/cuda-scratch/src/main.rs —— 示例代码
   fn main() {}
   ```

   然后连跑三次（每次都显式控制环境，避免机器上残留的 CUDA 变量干扰；首次构建还需 `cargo build` 先编译一次）：

   ```bash
   cd /tmp/cuda-scratch
   CUDA_HOME=$HOME CUDA_PATH=/tmp cargo build   # 预期 root=$HOME（PATH 被忽略）
   env -u CUDA_HOME CUDA_PATH=/tmp cargo build  # 预期 root=/tmp（HOME 缺席，PATH 顶上）
   env -u CUDA_HOME -u CUDA_PATH cargo build    # 预期 root=/usr/local/cuda（默认根）
   ```

3. **需要观察的现象**：每次构建的 warning 行 `[discover-lab] build.rs 已运行 epoch=... root=...`——因为每次环境变量的值都变了，build.rs 都会重跑（原因见 §4.3），`root=` 后面的值随环境切换。
4. **预期结果**：三次的 `root=` 分别是 `$HOME`、`/tmp`、`/usr/local/cuda`，与真值表 2、4、1 行一致。**待本地验证**（本讲义撰写环境未执行 cargo）。

#### 4.1.5 小练习与答案

1. **问**：机器上 `CUDA_HOME=/tmp`、`CUDA_PATH=/usr`，discover 会用哪个作 root？为什么？
   **答**：`/tmp`。`or_else` 只在 `CUDA_HOME` 读取失败（未设置/非 UTF-8）时才看 `CUDA_PATH`；这里 `CUDA_HOME` 有值，`CUDA_PATH` 从头到尾没被读。
2. **问**：`CUDA_HOME=""`（空字符串）和未设置 `CUDA_HOME`，行为一样吗？
   **答**：不一样。空串是 `Ok("")` → `Some("")` → root 为空路径并触发「不是目录」告警；未设置是 `Err` → 回落到 `CUDA_PATH` 乃至默认根。
3. **问**：为什么把「读环境变量」放进 `discover` 而不是让消费方自己读了传给 `from_root`？
   **答**：`from_root` 是私有函数（L91），环境策略被锁在库内一处；两个消费方无法各自发明回落规则，也无需重复实现。

### 4.2 无效根的告警路径

#### 4.2.1 概念说明

探测外部环境有三种失败策略：**硬失败**（panic/abort）、**软失败**（告警但继续）、**静默**（什么也不说）。discover 对「环境变量指向的根不是目录」选了第二种：发一条 `cargo:warning`，然后**继续用这个无效根**——不回落到下一个候选。

这个看似「不彻底」的选择有三层依据，全部能在源码里找到：

1. **无效根不等于死路**：nvcc 有 `$PATH` 回落（[L69](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L69) 的文档注释明说了这一点），PATH 上有 nvcc 时构建仍可能成功；
2. **空结果是合法状态**：目录探测统一经 `existing_deduped` 过滤不存在者，`CudaToolkit` 允许「空壳」存在，真正的失败点后移到真正需要它的地方；
3. **失败点后移更可诊断**：编译期最早发现「CUDA 不可用」的位置是 nvcc 实际执行或链接阶段，那里的报错直接指出缺什么；discover 若在此 panic，只会把「可能根本不需要 CUDA 的机器」也一并拦死。

对比之下，`find_package`（现在仅存于 `#[cfg(test)]`）对「找不到」选择 panic 并给出修复提示——那是「必须存在的头文件」的语义（测试里还在用 GDRCopy 的 `gdrapi.h` 当探针，见 [L274](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L274)）。**语义不同，失败策略不同**，两段代码并不矛盾。

#### 4.2.2 核心流程

环境根无效后的完整传导路径：

```
环境变量根存在但不是目录（L82 命中）
  └─ cargo:warning（L83-L85）……但 root 仍 = 该无效路径（L87 走 Some 分支）
       └─ from_root(root)（L91）
            ├─ root/bin/nvcc 不是文件（L92-L93）──▶ 裸 "nvcc"，交给 $PATH（L96）
            ├─ include / lib 候选目录全部不存在
            │       └─▶ existing_deduped（L145-L159）逐个 is_dir 过滤 ──▶ 空集
            └─ 结果：一个「空壳」CudaToolkit（root 无效、include_dirs/lib_dirs 全空）
                 ├─ 若 $PATH 上恰有 nvcc ──▶ .cu 编译仍可能成功
                 └─ 消费方调 link_search()（L132）发现 lib_dirs 空
                        └─▶ 第二条告警 "no CUDA library dir found under ..."（L134-L137）
```

两条告警一前一后：discover 阶段告诉你「你给的根不对」，link_search 阶段告诉你「这个根下面确实什么都没有」。

#### 4.2.3 源码精读

**(a) 告警触发条件的组合子写法。**
[pegainfer-build/src/lib.rs:L82-L86](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L82-L86) —— 用 `as_deref + filter` 一步筛出「有值但非目录」：

```rust
if let Some(root) = env_root.as_deref().filter(|root| !Path::new(root).is_dir()) {
    println!(
        "cargo:warning=CUDA root {root} (from CUDA_HOME/CUDA_PATH) is not a directory"
    );
}
```

逐步拆解：`env_root.as_deref()` 把 `Option<String>` 变 `Option<&str>`；`.filter(|root| !Path::new(root).is_dir())` 只在**有值且该值不是目录**时保留——`None`（两变量都没设）天然被过滤掉。所以这条告警有个重要盲区：**默认根 `/usr/local/cuda` 不受体检**。机器上没装 CUDA 且没设变量时（真值表场景 1），discover 阶段一条告警都没有，静默拿着不存在的 `/usr/local/cuda` 往下走。

**(b) 告警文案的信息缺口。**
文案写的是 `from CUDA_HOME/CUDA_PATH`——它**不区分**值来自哪个变量。因为 L79-L81 的链条只保留了值、丢掉了来源；想区分就得在读取时记录变量名。这是这段代码一个可改进的小诊断缺口（改动点就在 L79-L81）。

**(c) nvcc 的 `$PATH` 回落——「只告警不失败」的物质基础。**
[pegainfer-build/src/lib.rs:L69](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L69) —— 字段文档白纸黑字承诺了这个回落；[L92-L97](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L92-L97) 是实现：`bin/nvcc` 不是文件就存裸字符串 `"nvcc"`，未来真正调用它时由操作系统在 `$PATH` 里解析：

```rust
/// `{root}/bin/nvcc` when present, otherwise bare `nvcc` from `$PATH`.
pub nvcc: PathBuf,
```

**(d) 与两类「失败」风格的对照。**
[pegainfer-build/src/lib.rs:L44-L51](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L44-L51) —— `find_package` 找不到时 panic，文案里带 env 状态、候选清单和修复 Hint：

```rust
panic!(
    "{provider} build error: none of {check_files:?} found. \
     Looked at `${env_var}` ({env_status}) and default paths {default_paths:?}. \
     Hint: install the provider headers or set `{env_var}` to their install root.",
    /* ... */
)
```

[pegainfer-build/src/lib.rs:L132-L138](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L132-L138) —— `link_search` 对空 `lib_dirs` 又是「告警但继续」：空集时提醒一句，然后循环体一次都不执行，自然一条 `rustc-link-search` 也不发。三段代码放在一张表里：

| 函数 | 语义 | 找不到时 | 可见性 |
| --- | --- | --- | --- |
| `discover`（L76） | 尽力定位，允许落空 | 告警一次，继续用无效根 | `pub`，生产路径 |
| `link_search`（L132） | 输出链接目录 | 告警一次，不输出任何指令 | `pub`，生产路径 |
| `find_package`（L13） | 指定头文件必须存在 | panic + 修复提示 | `#[cfg(test)]`，仅测试 |

#### 4.2.4 代码实践：无效根 vs 有效目录，对比告警输出

1. **实践目标**：亲眼看到 L83-L85 那条告警的出现与消失。
2. **操作步骤**（沿用 4.1.4 的 `/tmp/cuda-scratch`；两次构建间环境变量的值都变了，所以 build.rs 都会重跑）：

   ```bash
   cd /tmp/cuda-scratch
   CUDA_HOME=/no/such/dir cargo build   # 无效根
   CUDA_HOME=/tmp cargo build           # 有效目录
   ```

3. **需要观察的现象**：第一次构建应出现两条 warning：`CUDA root /no/such/dir (from CUDA_HOME/CUDA_PATH) is not a directory` 和 `[discover-lab] ... root=/no/such/dir`；第二次只有 `[discover-lab] ... root=/tmp`，CUDA root 告警消失。
4. **预期结果**：告警的触发条件恰好是 `!is_dir()`，换成任何存在的目录（`/tmp`、`$HOME`）都不再触发。**待本地验证**。

#### 4.2.5 小练习与答案

1. **问**：两变量都未设置、机器上也没有 `/usr/local/cuda`，discover 阶段会产生几条告警？
   **答**：零条。L82 的 `filter` 对 `None` 直接放行，默认根不体检；第一条可能的告警要等到消费方调用 `link_search()`（L134-L137），或 nvcc 实际执行失败。
2. **问**：为什么 discover 不学 `find_package` 在无效时 panic，好让错误早暴露？
   **答**：nvcc 有 `$PATH` 回落（L69/L92-L97）、空目录集是合法状态（L145-L159 过滤），无效根并不必然导致构建失败；此时 panic 会误伤「不需要完整工具包、只靠 PATH 上 nvcc」的机器。失败点后移到真正用到缺失物的地方，报错反而更准确。
3. **问**：无效根告警之后，最终传给 `from_root` 的是哪个路径？
   **答**：仍是那个无效路径。L87 的 `Some` 分支不体检、不回落——告警只是提示，不参与决策。

### 4.3 rerun-if-env-changed 的声明时机

#### 4.3.1 概念说明

`cargo:rerun-if-env-changed=NAME` 向 Cargo 声明：「**NAME 的值**属于本 build.rs 的指纹」。值变了（包括 unset ↔ set 的切换）才重跑脚本；值没变就不跑——**哪怕你把 CUDA 安装目录整个换掉**。

配套的一条规则（见 §2.4）：一旦 build.rs 发射过任何 rerun-if 指令，Cargo 就不再用「包内任何文件变化」作默认指纹。所以这两行 `println!` 实际上把消费方的缓存策略**整体切换**成了「只认声明的理由」。

时机问题有两层。**机制层**：`cargo:` 指令是 build.rs 进程 stdout 上的普通文本行，Cargo 在脚本跑完后统一解析，所以指令打在函数第一行还是最后一行，对 Cargo 没有区别。**契约层**：把声明放在函数体最顶部、紧贴 L79-L81 那两行环境变量读取，是「依赖先于行为」的写法——读者一眼看到「这个函数的输出依赖这两个变量，且依赖已被声明」。

更重要的设计决策是**谁来声明**。这两条指令由库函数 discover() 自己发射，而不是要求每个消费方的 build.rs 记得声明。契约由库持有：消费方调一行 `discover()`，缓存正确性自动到位，不存在「忘了声明导致 stale」的人为失误面。

#### 4.3.2 核心流程

Cargo 对某 crate 的 build.rs 做重跑判定的决策树：

```
build.rs 自身（或其 [build-dependencies]，如 pegainfer-build）重新编译了吗？
  ├─ 是 ──▶ 必重跑
  └─ 否 ──▶ 该 build.rs 历史上发射过 rerun-if 指令吗？
        ├─ 否（默认指纹）──▶ 包内任何文件变化即重跑
        └─ 是 ──▶ 仅当任一声明的原因成立才重跑：
              ├─ rerun-if-changed 指向的路径有变化
              └─ rerun-if-env-changed 指向的变量的【值】有变化（含 unset↔set）
```

以 `pegainfer-cupti` 为例，其 build.rs 的重跑原因集合恰好三项——discover() 带来的两个变量，加上它自己声明的源文件：

```
{ CUDA_HOME 值变化 } ∪ { CUDA_PATH 值变化 } ∪ { csrc/range_profiler.cpp 变化 }
```

#### 4.3.3 源码精读

**(a) 声明先于读取。**
[pegainfer-build/src/lib.rs:L77-L78](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L77-L78) —— 函数的前两行、L79 读取之前：

```rust
println!("cargo:rerun-if-env-changed=CUDA_HOME");
println!("cargo:rerun-if-env-changed=CUDA_PATH");
```

注意 discover 的文件系统探测（`is_dir`）**没有**对应的 `rerun-if-changed` 声明——指纹只覆盖两个变量的值，不覆盖目录内容。这正是「换了 CUDA 安装但环境变量没变 → 不重跑 → 沿用旧输出」这一缓存风险的根源（深入分析与实验在 u3-l3）。

**(b) 消费方自动继承契约。**
[pegainfer-cupti/build.rs:L5](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-cupti/build.rs#L5) —— 第一行就是 `discover()`，两条 env 指令随之进入 Cargo 的指纹；[L33](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-cupti/build.rs#L33) —— 它只为自己的源文件追加声明，`pegainfer-kernels/build.rs`（u1-l1 定位过其 discover 调用点）同理：

```rust
println!("cargo:rerun-if-changed=csrc/range_profiler.cpp");
```

**(c) 指令只在 build.rs 进程里有效。**
两个佐证：其一，pegainfer-build 自己**没有** build.rs——这两条指令只在消费方的构建脚本进程里被 Cargo 解析；其二，lib.rs 的 7 个测试全部只调 `from_root`（[L201](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L201)、[L217](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L217)、[L233](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L233)、[L254](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L254)、[L265](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L265)）或 `find_package`，从不调 `discover`。顺带一提，[L19](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L19) 的 `find_package` 也有一模一样的参数化写法 `println!("cargo:rerun-if-env-changed={}", env_var);`——但测试进程打印的 `cargo:` 前缀行不会被 Cargo 当作构建指令解析（只会在 `--nocapture` 下作为普通输出可见），机制上是惰性的。

#### 4.3.4 代码实践：时间戳探针验证「值不变则不重跑」

1. **实践目标**：用 4.1.4 实验室里 build.rs 打印的 `epoch=<秒>` 探针，证明重跑确实由变量值驱动。
2. **操作步骤**（沿用 `/tmp/cuda-scratch`；每步之后看输出里 `[discover-lab]` 行是否出现、秒数是否更新）：

   ```bash
   cd /tmp/cuda-scratch
   CUDA_HOME=/tmp cargo build        # 步骤 A：基线（值与上一步实验可能不同，应重跑）
   CUDA_HOME=/tmp cargo build        # 步骤 B：值未变 → 预期【不】重跑
   touch src/main.rs
   CUDA_HOME=/tmp cargo build        # 步骤 C：改的是包内源码 → 预期仍不重跑
   CUDA_HOME=$HOME cargo build       # 步骤 D：值变化 → 预期重跑
   touch build.rs
   CUDA_HOME=$HOME cargo build       # 步骤 E：脚本自身重编译 → 预期重跑
   ```

3. **需要观察的现象**：步骤 B、C 的输出里没有 `[discover-lab]` 行（脚本被跳过，main.rs 正常重编译）；步骤 D、E 重新出现该行且秒数更新。
4. **预期结果**：与 §4.3.2 决策树一致——尤其步骤 C 验证了「发射过 rerun-if 之后，包内文件变化不再触发重跑」；步骤 E 是日常开发中让探测重新生效的快捷手段（等价于 `cargo clean -p cuda-scratch`）。**待本地验证**。

#### 4.3.5 小练习与答案

1. **问**：若某消费方不调 `discover()` 而是自己 `env::var("CUDA_HOME")` 读根，它还欠 Cargo 什么？
   **答**：一条 `println!("cargo:rerun-if-env-changed=CUDA_HOME")`。否则变量值变化不会触发重跑；而且只要它发射过任何其他 rerun-if 指令，连默认指纹都没了，stale 风险更大。
2. **问**：把 `/usr/local/cuda` 软链从 12.4 重指到 12.6（两个环境变量始终没设），接下来 `cargo build` 会发生什么？
   **答**：指纹没变（变量值恒为 unset，也没有任何 rerun-if-changed 覆盖该目录），build.rs 不重跑，沿用上次输出。需要 `touch build.rs`、`cargo clean`，或临时设置再改变量值来强制重跑。
3. **问**：把 L77-L78 挪到函数末尾，行为会变吗？
   **答**：Cargo 解析不依赖顺序，行为不变；变的是可读性——依赖声明脱离了它对应的读取处，「契约先于行为」的排版意义丢失。

## 5. 综合实践

**任务：产出 discover 的完整行为对照表。** 把本讲三个模块串成一张可复现的表——这既是本讲的收口，也是你日后排查「CUDA 找不到」类构建问题的第一手依据。

1. **实践目标**：用同一个实验室、连续六次构建，覆盖真值表全部六行 + 告警出现/消失 + 重跑/不重跑三类现象。
2. **操作步骤**（沿用 4.1.4 的 `/tmp/cuda-scratch`，从零开始更干净）：

   ```bash
   cd /tmp/cuda-scratch
   rm -rf target && env -u CUDA_HOME -u CUDA_PATH cargo build  # R1 场景1
   rm -rf target && CUDA_HOME=$HOME CUDA_PATH=/tmp cargo build  # R2 场景2
   rm -rf target && CUDA_HOME=/bad/path CUDA_PATH=/tmp cargo build  # R3 场景3
   rm -rf target && env -u CUDA_HOME CUDA_PATH=/tmp cargo build # R4 场景4
   rm -rf target && env -u CUDA_HOME CUDA_PATH=/bad/path cargo build  # R5 场景5
   rm -rf target && CUDA_HOME= CUDA_PATH=/tmp cargo build       # R6 场景6（注意 CUDA_HOME= 赋空串）
   CUDA_HOME= cargo build    # R7：紧接 R6，值未变 —— 验证不重跑
   touch build.rs && CUDA_HOME=/tmp cargo build  # R8：强制重跑 —— 验证探针回归
   ```

   说明：`rm -rf target` 让每次都从冷缓存出发，逐行复现真值表；R7、R8 不删 target，专门验证 §4.3 的指纹行为。

3. **需要观察的现象**：R1–R6 每次都有 `[discover-lab]` 行，`root=` 与告警随环境变化；R7 无该行；R8 该行回归。
4. **预期结果**：填出下表（`root=` 与告警列按源码推得，全部**待本地验证**）：

   | 运行 | root= 预期 | CUDA root 告警 | 重跑？ |
   | --- | --- | --- | --- |
   | R1 | `/usr/local/cuda` | 无 | 是（冷启动） |
   | R2 | `$HOME` | 无 | 是 |
   | R3 | `/bad/path` | **有** | 是 |
   | R4 | `/tmp` | 无 | 是 |
   | R5 | `/bad/path` | **有** | 是 |
   | R6 | （空） | **有**（空串非目录） | 是 |
   | R7 | —（未运行） | — | **否**（值未变） |
   | R8 | `/tmp` | 无 | 是（脚本重编译） |

## 6. 本讲小结

- 回落链 `CUDA_HOME → CUDA_PATH → /usr/local/cuda` 是**读取层面**的：`or_else` 只在 `env::var` 报错（未设置/非 UTF-8）时才轮到下一个；已设置的坏 `CUDA_HOME` 会遮蔽好的 `CUDA_PATH`，空串也算「已设置」。
- 环境根不是目录时只发一条 `cargo:warning`，**无效根仍被采用**：nvcc 回落裸 `"nvcc"` 走 `$PATH`、目录集经 `existing_deduped` 过滤成空——「空壳工具包」是合法状态；默认根 `/usr/local/cuda` 不受体检，两变量全空时 discover 阶段零告警。
- 三种失败策略各就各位：`discover` 尽力而为（告警继续）、`link_search` 对空集二次提醒、`find_package`（测试专用）对缺失 panic 并给修复提示——语义决定策略。
- `rerun-if-env-changed` 以**变量的值**为指纹：值不变不重跑；发射过任何 rerun-if 指令后，Cargo 放弃「包内文件变化」的默认指纹。两条指令由库函数在读取环境变量**之前**发射，消费方一行 `discover()` 自动继承缓存契约。
- 指纹不覆盖文件系统状态——换 CUDA 安装而环境变量不变会沿用陈旧输出；`touch build.rs`、`cargo clean` 或改变量值可强制重跑（深挖在 u3-l3）。

## 7. 下一步学习建议

discover 定下根之后，剩下的问题全在 `from_root`：它怎么把根目录拼成候选目录集合。建议顺序：

1. **u2-l2（target_dirs）**：先读 [L56-L63](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L56-L63)——`CARGO_CFG_TARGET_ARCH` 与宿主架构回落、aarch64 的 `aarch64-linux`/`sbsa-linux` 双候选，是从_root 依赖的第一个零件。
2. **u2-l3（from_root）**：精读 [L91-L121](https://github.com/openinfer-project/openinfer/blob/223206b7ddbc7441063741484d9aa610d5ed9bb0/pegainfer-build/src/lib.rs#L91-L121)，看 classic / conda / HPC SDK 三种布局如何被同一套候选逻辑覆盖。
3. **延伸阅读**：Cargo 手册 Build Scripts 一章的 "Outputs of the build script"（rerun-if 指令与指纹的官方定义），可作为本讲 §4.3 的一手对照；缓存正确性的完整实验则在 **u3-l3** 展开。
