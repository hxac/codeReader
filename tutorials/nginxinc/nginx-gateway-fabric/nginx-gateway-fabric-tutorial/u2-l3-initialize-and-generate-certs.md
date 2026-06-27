# 初始化命令与证书生成

## 1. 本讲目标

本讲承接 [u2-l1 命令行子命令总览](u2-l1-cli-subcommands-overview.md)，把两个一次性辅助子命令 `initialize` 和 `generate-certs` 讲透。学完本讲，你应当能够：

- 说清 `initialize` 命令为什么必须作为数据面 Pod 的 **init 容器** 运行，以及它到底往磁盘写了什么。
- 看懂 `generate-certs` 如何自签出一套 CA / 服务端 / 客户端证书，并把它们写回 `server-tls` 与 `agent-tls` 两个 Secret。
- 理解控制面与数据面之间 **mTLS** 的信任关系：CA 是谁、服务端证书归谁、客户端证书归谁。
- 了解 NGINX Plus 场景下 `initialize` 额外生成的 `deployment_ctx.json`（授权上下文）从哪来、用在哪。
- 独立梳理出一张「证书用途表」，把三种证书与服务端/客户端角色对上号。

## 2. 前置知识

在进入源码前，先用通俗语言建立三个概念。

### 2.1 为什么要 init 容器？

Kubernetes 的 Pod 可以有多个容器，其中 **init 容器** 一定在主容器之前、按顺序运行，且必须成功退出（exit 0）才会启动后续容器。NGINX 数据面容器启动时要读 `/etc/nginx/conf.d/` 等目录下的配置文件，这些文件必须「在 NGINX 进程拉起来之前」就位。所以「把初始文件摆好」这件事天然适合放进 init 容器。`initialize` 命令就是干这件事的。

> 小知识：init 容器只跑一次就退出，而主容器（NGINX、Agent）是长驻进程。这是分辨「init 容器命令」与「主进程命令」最直观的线索。

### 2.2 什么是 mTLS（双向 TLS）？

普通 HTTPS 是「客户端校验服务端」。**mTLS（mutual TLS）** 则是「双方互相校验」：

- 服务端出示自己的证书，客户端用 **CA 证书** 校验它；
- 客户端也出示自己的证书，服务端用 **同一个 CA 证书** 校验它。

NGF 的控制面（Go 进程，跑 gRPC 服务端）和数据面（NGINX Agent，跑 gRPC 客户端）之间就是 mTLS。这套证书由 `generate-certs` 命令一次性自签出来，再用同一个 **自签 CA** 给双方签发各自的终端证书。

### 2.3 证书里的 SAN 是什么？

**SAN（Subject Alternative Name）** 是证书里「这张证书到底代表哪些名字」的字段。TLS 握手时，校验方会检查「对端报上来的 DNS 名 / IP」是否出现在对端证书的 SAN 里。所以给服务端证书填的 SAN 必须是客户端实际用来连接的那个 DNS 名，否则校验失败。这一点在后面看 `serverDNSNames` 时会再次遇到。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [cmd/gateway/initialize.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/initialize.go) | `initialize` 命令的核心逻辑：拷贝初始文件、（仅 Plus）生成 deployment context。 |
| [cmd/gateway/certs.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/certs.go) | `generate-certs` 的核心逻辑：自签 CA / 服务端 / 客户端证书，写回 Secret。 |
| [cmd/gateway/commands.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go) | 两个命令的 cobra 装配（flag、RunE）以及默认 Secret 名常量。 |
| [cmd/gateway/validation.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/validation.go) | `validateCopyArgs`：校验 initialize 的 `--source` 与 `--destination` 数量一致。 |
| [internal/framework/file/file.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/file/file.go) | `OSFileManager` 接口与 `Write` 工具，把文件落到磁盘。 |
| [internal/controller/nginx/config/generator.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/generator.go) | `GenerateDeploymentContext`：把 Plus 授权上下文序列化成 `deployment_ctx.json`。 |
| [internal/controller/state/dataplane/types.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/types.go) | `DeploymentContext` 结构定义。 |
| [internal/controller/nginx/agent/grpc/grpc.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/grpc/grpc.go) | 控制面 gRPC 服务端如何消费 `server-tls` 做 mTLS（佐证证书用途）。 |
| charts/nginx-gateway-fabric/templates/certs-job.yaml、deployment.yaml | Helm 模板：证明 `generate-certs` 跑在 pre-install/pre-upgrade Job，`initialize` 跑在 init 容器。 |

---

## 4. 核心概念与源码讲解

### 4.1 initialize 命令：init 容器写初始文件

#### 4.1.1 概念说明

`initialize` 是一个**幂等的、一次性**命令：它做两件事——

1. **拷贝一批初始文件**：把命令行传入的 `--source` 文件，逐个拷到 `--destination` 目录里。
2. **（仅 NGINX Plus）生成 deployment context**：把集群/安装标识序列化成 `deployment_ctx.json`，供 NGINX Plus 的 licensing/mgmt 使用。

它被设计成「跑完就退出」，因此天然落在 init 容器里：等它把静态配置文件和（Plus 时的）授权上下文都摆好，主容器里的 NGINX 才会启动。

#### 4.1.2 核心流程

`initialize` 的执行流程可以用下面的伪代码描述：

```
输入: fileToCopy 列表, plus 标志, podUID, clusterUID

1. for each fileToCopy:
       copyFile(src -> destDir)        # 逐个拷贝
2. if 不是 Plus:
       记日志 "Finished initializing configuration"
       return                          # 非 Plus 到此结束
3. 构造 DeploymentContext{ podUID, clusterUID, integration="ngf" }
4. fileGenerator.GenerateDeploymentContext(depCtx) -> depCtxFile
5. file.Write(depCtxFile)              # 落盘 deployment_ctx.json
6. 记日志，return
```

关键点：**第 1 步对 OSS 和 Plus 都执行；第 3~5 步只有 Plus 才走**。这解释了为什么非 Plus 环境不会出现 `deployment_ctx.json`。

#### 4.1.3 源码精读

先看 `initialize` 的配置结构与主函数。`initializeConfig` 把所有依赖都聚合进来，是一个典型的「构造一次、传入函数」的小对象：

[cmd/gateway/initialize.go:24-32](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/initialize.go#L24-L32) — `initializeConfig` 结构：`fileManager`（文件 I/O）、`fileGenerator`（配置生成器，用于 Plus）、`logger`、`podUID`/`clusterUID`、`copy` 列表、`plus` 开关。

[cmd/gateway/initialize.go:34-64](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/initialize.go#L34-L64) — 主函数 `initialize`：先循环拷贝（第 35-39 行），随后判断 `if !cfg.plus` 就直接返回（第 41-44 行）；只有 Plus 才构造 `DeploymentContext` 并写出文件（第 46-59 行）。

注意第 46-50 行构造 `DeploymentContext` 时，把 `podUID` 和 `clusterUID` 的**指针**赋给 `InstallationID` / `ClusterID`，并固定写入 `Integration: "ngf"`（来自常量 `integrationID = "ngf"`，见 [cmd/gateway/initialize.go:15-17](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/initialize.go#L15-L17)）。

再看单文件拷贝的实现：

[cmd/gateway/initialize.go:66-88](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/initialize.go#L66-L88) — `copyFile`：打开源文件 → 在目标目录下用 `filepath.Base(src)` 作文件名创建目标文件 → 拷贝内容 → `Chmod` 设为普通文件权限 `0o644`。

这里的 `osFileManager` 是一个接口，而不是直接调用 `os` 包：

[internal/framework/file/file.go:55-67](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/file/file.go#L55-L67) — `OSFileManager` 接口暴露 `Open/Create/Chmod/Write/Copy`。用接口而非直接 `os.*` 是为了能在测试里注入 fake 文件系统（见 `initialize_test.go`）。默认实现由 [cmd/gateway/commands.go:882](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L882) 的 `file.NewStdLibOSFileManager()` 提供。

最后看命令装配。`createInitializeCommand` 把 flag 与 `initialize` 连起来：

[cmd/gateway/commands.go:834-917](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L834-L917) — `createInitializeCommand`。要点：
- `--source`（`srcFiles`）和 `--destination`（`destDirs`）都是 `StringSliceVar`，第 914 行用 `MarkFlagsRequiredTogether` 强制两者必须同时出现。
- RunE 里第 848 行先调 `validateCopyArgs` 做长度一致性校验。
- 第 852-860 行从环境变量 `POD_UID`、`CLUSTER_UID` 读取标识——这两个值在 Pod 里来自 downward API（pod.metadata.uid 等）。
- 第 881-889 行装配 `initializeConfig`，其中 `fileGenerator` 用 `ngxConfig.NewGeneratorImpl(plus, nil, logger)` 构造，把 `plus` 透传进去。

长度校验函数本身很简单：

[cmd/gateway/validation.go:266-278](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/validation.go#L266-L278) — `validateCopyArgs`：要求 `srcFiles` 与 `destDirs` 数量相等、且都非空。这保证了「按数组下标一一对应拷贝」是安全的（见 commands.go 第 873-879 行把两个切片 zip 成 `[]fileToCopy`）。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：确认 `initialize` 的「源 → 目」拷贝是按下标一一对应的。

**操作步骤**：

1. 打开 [cmd/gateway/commands.go:873-879](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L873-L879)，看 `for i, src := range srcFiles` 如何把 `srcFiles[i]` 与 `destDirs[i]` 配对。
2. 打开 [cmd/gateway/validation.go:266-278](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/validation.go#L266-L278)，确认「数量相等」是 zip 安全的前提。
3. 想一个反例：如果用户传了 `--source=a,b --destination=x`，会在哪一行报错、报什么错？

**需要观察的现象 / 预期结果**：`--source` 与 `--destination` 长度不等时，`validateCopyArgs` 返回 `"source and destination must have the same number of elements"`，命令在拷贝前就退出，不会拷半个。

> 待本地验证：可在本地编译 `make build` 后执行 `./build/out/gateway initialize --source=/a,/b --destination=/x`，观察是否输出上述错误。（初始化还会因缺少 `POD_UID` 等环境变量报错，但 length 校验在前，应优先触发。）

#### 4.1.5 小练习与答案

**练习 1**：如果把 `initialize` 从 init 容器改成主容器，最直接的后果是什么？

**参考答案**：init 容器的语义是「先跑完、再启动主容器」。改成主容器后，文件拷贝与 NGINX 进程会并发/顺序不确定地运行，NGINX 可能在初始配置文件就位之前就启动，读到空目录或旧文件，导致启动失败或行为异常。

**练习 2**：非 Plus 环境下，`deployment_ctx.json` 会被生成吗？为什么？

**参考答案**：不会。`initialize` 在 [cmd/gateway/initialize.go:41-44](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/initialize.go#L41-L44) 用 `if !cfg.plus` 提前 return，跳过了第 46-59 行的 `GenerateDeploymentContext` 与落盘逻辑。

---

### 4.2 generate-certs 命令：自签证书与 Secret 创建

#### 4.2.1 概念说明

`generate-certs` 负责为「控制面 ↔ 数据面」的 mTLS 准备好一整套证书材料，并写回两个 Kubernetes Secret：

- **`server-tls`**：控制面 gRPC **服务端**持有的证书 + CA。
- **`agent-tls`**：数据面 NGINX Agent（gRPC **客户端**）持有的证书 + CA。

两个 Secret 都包含同一份自签 **CA**（`ca.crt`），这就构成了 mTLS 的「共同信任根」：双方都用这份 CA 校验对端。

它在 Helm 安装时由 `pre-install, pre-upgrade` 钩子触发，跑成一个一次性 **Job**（见 [certs-job.yaml:116](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/charts/nginx-gateway-fabric/templates/certs-job.yaml#L116)），因此必须在控制面 Deployment 启动前就把 Secret 生成好。

#### 4.2.2 核心流程

证书生成的整体流程：

```
1. generateCA()
       生成 2048 位 RSA 私钥 -> 自签 CA 证书（IsCA=true, 3 年有效）
       返回 caCertPEM, caKeyPEM
2. tls.X509KeyPair(caCertPEM, caKeyPEM)   # 把 CA 解析成可签发的 keypair
3. generateCert(CA, serverDNSNames)       # 用 CA 签服务端证书
4. generateCert(CA, clientDNSNames)       # 用 CA 签客户端证书
5. createSecrets(...)
       server-tls Secret = { ca.crt, tls.crt=server, tls.key=server }
       agent-tls  Secret = { ca.crt, tls.crt=client, tls.key=client }
       Secret 已存在且 --overwrite=false -> 跳过；否则按需 Create / Update
```

证书有效期与签发参数是固定常量：

- 有效期：\( expiry = 365 \times 3 \times 24 \) 小时 = **3 年**（[certs.go:27](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/certs.go#L27)）。
- 密钥：RSA **2048 位**。
- SAN 命名规则：
  - 服务端：\( serverDNSNames = service \cdot namespace \cdot serverTLSDomain \)，例如 `nginx-gateway.nginx-gateway.svc`。
  - 客户端：\( clientDNSNames = {}^*\cdot clusterDomain \)，例如 `*.cluster.local`。

#### 4.2.3 源码精读

先看常量、主体名与结果容器：

[cmd/gateway/certs.go:26-45](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/certs.go#L26-L45) — `expiry`/`defaultDomain` 常量、`subject`（CN=`nginx-gateway`，组织 F5/NGINX）、`certificateConfig` 结构（一次性装 CA、server cert/key、client cert/key 五块 PEM）。

[cmd/gateway/certs.go:48-76](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/certs.go#L48-L76) — `generateCertificates`：先 `generateCA()`，再用同一个 CA keypair 分别签服务端、客户端证书。注意两次 `generateCert` 的差别只在传入的 DNS 名字（第 59 行 server、第 64 行 client）。

CA 的生成是这套证书体系的「根」：

[cmd/gateway/certs.go:78-110](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/certs.go#L78-L110) — `generateCA`：第 84-92 行构造 `x509.Certificate`，关键标志位是 `KeyUsageCertSign`（允许签发下游证书）、`IsCA: true`、`BasicConstraintsValid: true`；第 94 行 `x509.CreateCertificate(rand.Reader, ca, ca, ...)` 是**自签**（issuer 与 subject 都是 `ca` 自己）。`SubjectKeyId` 由私钥模数经 SHA-1 得到（见下方 `subjectKeyID`）。

> 关于 SHA-1：源码第 7、152 行标注了 `//nolint:gosec` 并注释「using sha1 in this case is fine」。因为 SHA-1 只用于生成 `SubjectKeyID`（一个标识符），并非用于证书签名算法，所以不构成安全弱点。这是阅读 Go 安全代码时常见的取舍。

终端证书的签发：

[cmd/gateway/certs.go:112-148](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/certs.go#L112-L148) — `generateCert`：新建一对 RSA 密钥，证书的 issuer 是 CA（第 127、132 行用 `caCert` 作 parent、`caKeyPair.PrivateKey` 签名），`DNSNames` 设为传入的 SAN。这是「CA 签终端证书」的标准流程。

SAN 命名函数：

[cmd/gateway/certs.go:157-167](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/certs.go#L157-L167) — `serverDNSNames` 拼出控制面 Service 的全限定域名；`clientDNSNames` 用通配符 `*.<clusterDomain>` 覆盖整个集群域。

证书生成后，`createSecrets` 把它们写进两个 Secret，并处理「已存在」的情况：

[cmd/gateway/certs.go:169-232](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/certs.go#L169-L232) — `createSecrets`。要点：
- 两个 Secret 都是 `corev1.SecretTypeTLS` 类型，数据键沿用 [secrets.go:37-50](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/shared/secrets/secrets.go#L37-L50) 的常量：`ca.crt`（`CAKey`）、`tls.crt`（`TLSCertKey`）、`tls.key`（`TLSKeyKey`）。
- 第 184-189 行：`server-tls` 的 `tls.crt/tls.key` 放的是**服务端**证书与私钥。
- 第 196-202 行：`agent-tls` 的 `tls.crt/tls.key` 放的是**客户端**证书与私钥。
- 两个 Secret 的 `ca.crt` 是**同一份** CA（`certConfig.caCertificate`）。
- 第 209-228 行是「Get → 不存在则 Create；存在则按 `overwrite` 决定是否 Update」的幂等逻辑。`overwrite=false` 且 Secret 已存在时，第 218-221 行记日志后跳过；`overwrite=true` 时只有当 `Data` 真的变化（`reflect.DeepEqual`）才 Update，避免无谓写入。

最后看命令装配与 flag：

[cmd/gateway/commands.go:719-832](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L719-L832) — `createGenerateCertsCommand`。要点：
- 默认 Secret 名来自常量 [cmd/gateway/commands.go:39-40](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L39-L40)：`server-tls` / `agent-tls`。
- RunE 第 758 行从环境变量 `POD_NAMESPACE` 取命名空间（Job 里通过 downward API 注入，见 [certs-job.yaml:137-141](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/charts/nginx-gateway-fabric/templates/certs-job.yaml#L137-L141)）。
- 第 763 行调用 `generateCertificates(serviceName, namespace, clusterDomain, serverTLSDomain)`，把四个 flag（`--service`/`--cluster-domain`/`--server-tls-domain`/命名空间）汇成签名入参。
- 第 773-781 行调用 `createSecrets`，传入 `--server-tls-secret` / `--agent-tls-secret` 的名字与 `--overwrite` 开关。

#### 4.2.4 代码实践（本讲主实践）：梳理证书用途表

**实践目标**：把三种证书材料（CA、服务端证书、客户端证书）与「谁持有、谁校验」对应清楚，形成一张表。

**操作步骤**：

1. 阅读三处源码确认事实：
   - 生成逻辑 [cmd/gateway/certs.go:48-76](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/certs.go#L48-L76)：CA 只有一份；server 与 client 由同一个 CA 签发。
   - 落盘逻辑 [cmd/gateway/certs.go:178-202](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/certs.go#L178-L202)：两个 Secret 各自装了什么。
   - 消费逻辑 [internal/controller/nginx/agent/grpc/grpc.go:30-32](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/grpc/grpc.go#L30-L32) 与 [grpc.go:184](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/grpc/grpc.go#L184)：控制面 gRPC 服务端从 `/var/run/secrets/ngf/` 读 `ca.crt/tls.crt/tls.key`，并用 `ClientAuth: tls.RequireAndVerifyClientCert` **强制校验客户端证书**。
2. 用下表把映射关系填全（答案见下）。

**需要观察的现象 / 预期结果**：得到如下「证书用途表」：

| 角色 | 证书材料 | 所在 Secret | 数据键 | 持有方（运行位置） | 对端如何校验 | SAN |
| --- | --- | --- | --- | --- | --- | --- |
| 信任根（CA） | 自签 CA 证书 | `server-tls` **和** `agent-tls`（都含） | `ca.crt` | 不直接参与握手，作为共同信任根分发 | 双方各自用 `ca.crt` 构建信任池校验对端 | CN=`nginx-gateway` |
| 服务端证书 | server cert + server key | `server-tls` | `tls.crt` / `tls.key` | **控制面** `nginx-gateway` 容器（gRPC 服务端，`:8443`） | 数据面 Agent 用 CA 校验，并匹配 DNS 名 | `<service>.<ns>.<serverTLSDomain>`，如 `nginx-gateway.nginx-gateway.svc` |
| 客户端证书 | client cert + client key | `agent-tls` | `tls.crt` / `tls.key` | **数据面** NGINX Agent（gRPC 客户端） | 控制面用 CA + `RequireAndVerifyClientCert` 强制校验 | `*.<clusterDomain>`，如 `*.cluster.local` |

**关键结论**：
- **服务端是控制面**：它持有 `server-tls`，对外提供 gRPC（`:8443`）。
- **客户端是数据面 Agent**：它持有 `agent-tls`，主动 dial 控制面。
- **两端的「对端校验」都用同一份 `ca.crt`**——这正是把 `ca.crt` 同时写进两个 Secret 的原因。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `--server-tls-domain` 从默认的 `svc` 改成 `cluster.local`，服务端证书的 SAN 会变成什么？这会带来什么风险？

**参考答案**：SAN 会变成 `<service>.<namespace>.cluster.local`。风险在于：数据面 Agent 连接控制面时使用的 DNS 名若不是这个新名字（默认连的是 `<service>.<ns>.svc`），TLS 校验会因 SAN 不匹配而失败，导致 Agent 无法连上控制面。所以 `--server-tls-domain` 必须与 Agent 实际连接控制面时用的域名后缀一致。

**练习 2**：第二次执行 `generate-certs`（未带 `--overwrite`）时，已存在的 Secret 会被覆盖吗？

**参考答案**：不会。[certs.go:218-221](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/certs.go#L218-L221) 在 `overwrite=false` 且 Secret 已存在时记录日志后 `continue` 跳过。这是为了避免 Helm `pre-upgrade` 钩子意外覆盖用户手动管理（或外部 cert-manager 管理）的证书。需要强制更新时才显式传 `--overwrite`，且仅当 `Data` 真有变化才写（第 223-227 行）。

**练习 3**：为什么控制面 gRPC 服务端要设 `RequireAndVerifyClientCert`，而不是 `VerifyClientCertIfGiven`？

**参考答案**：mTLS 的安全前提是「没有合法客户端证书就拒绝连接」。`RequireAndVerifyClientCert` 表示「必须出示且必须校验通过」，确保只有持有 `agent-tls` 客户端证书（由同一 CA 签发）的数据面 Agent 才能连上控制面；若改成「如果给了就校验」，则任何不提供证书的连接都能绕过身份核验，mTLS 就退化成单向 TLS。见 [grpc.go:184](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/grpc/grpc.go#L184)。

---

### 4.3 NGINX Plus 场景：deployment context 的生成

#### 4.3.1 概念说明

NGINX Plus 是商业版，运行时需要向 F5 上报「这份 Plus 跑在哪个集群、哪个安装实例上」的授权上下文（用于 licensing / usage report）。这个上下文被序列化成一个 JSON 文件 `deployment_ctx.json`，由 `initialize` 命令在 init 容器里生成，最终被 NGINX Plus 的 mgmt 配置块 include 使用。

因此 `initialize` 在 Plus 下多干一件事：把 `podUID`/`clusterUID` 序列化进 `deployment_ctx.json`。

#### 4.3.2 核心流程

```
1. initialize() 在拷贝完文件后，因 cfg.plus==true 继续:
2. 构造 dataplane.DeploymentContext{
       InstallationID: &podUID,
       ClusterID:      &clusterUID,
       Integration:    "ngf",
   }
3. GeneratorImpl.GenerateDeploymentContext(depCtx):
       json.Marshal(depCtx) -> bytes
       包装成 agent.File{ Meta.Name = "<mainIncludes>/deployment_ctx.json",
                          Permissions = "0644" }
4. file.Write(depCtxFile)   # 落盘到 mainIncludes 目录
```

其中 `mainIncludesFolder` 是 NGINX 主配置 include 的目录（即 `/etc/nginx/main-includes` 一类），`deployment_ctx.json` 会落在这里，被 nginx.conf 的 mgmt 块引用。

#### 4.3.3 源码精读

数据结构定义：

[internal/controller/state/dataplane/types.go:834-845](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/types.go#L834-L845) — `DeploymentContext`：`ClusterID`（kube-system 命名空间 UID）、`InstallationID`（NGF 部署实例 ID）、`ClusterNodeCount`、`Integration`（固定 `"ngf"`）。注意带 `json` tag，因为要序列化成文件，并且指针字段带 `omitempty`。

序列化与封装：

[internal/controller/nginx/config/generator.go:161-180](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/generator.go#L161-L180) — `GenerateDeploymentContext`：第 164 行 `json.Marshal(depCtx)`，第 169-177 行把字节流封装成 `agent.File`，路径为 `mainIncludesFolder + "/deployment_ctx.json"`，权限 `RegularFileMode`（`0644`）。注释第 161-162 行说明它「被 init 容器进程使用」，正是 `initialize` 调用的入口。

落盘：

[cmd/gateway/initialize.go:52-59](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/initialize.go#L52-L59) — 调用 `cfg.fileGenerator.GenerateDeploymentContext(depCtx)` 拿到文件对象，再用 `file.Write(fileManager, file.Convert(depCtxFile))` 落盘。这里 `file.Convert` 把 `agent.File` 转成内部 `file.File` 类型（见 [internal/framework/file/file.go:116-134](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/file/file.go#L116-L134)），并根据权限字符串映射成 `TypeRegular`（`0644`）或 `TypeSecret`（`0640`）。`deployment_ctx.json` 是 `0644`，故落盘为普通文件权限。

`file.Write` 会按文件类型设权限后写内容：

[internal/framework/file/file.go:69-107](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/file/file.go#L69-L107) — `Write`：Create → 按 `Type` Chmod（普通文件 `0o644`、密钥文件 `0o640`）→ Write，并安全地关闭文件、聚合错误。

#### 4.3.4 代码实践（源码阅读型）

**实践目标**：确认 `deployment_ctx.json` 只在 Plus 下生成，并理解它的字段来源。

**操作步骤**：

1. 在 [cmd/gateway/initialize.go:41-59](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/initialize.go#L41-L59) 确认：非 Plus 在第 44 行 return，Plus 才走到第 52 行。
2. 在 [internal/controller/state/dataplane/types.go:836-845](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/types.go#L836-L845) 查 `InstallationID` / `ClusterID` 的注释，确认 `podUID` 与 `clusterUID` 的语义。
3. 回到 [cmd/gateway/commands.go:852-860](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L852-L860)，确认这两个 UID 来自环境变量 `POD_UID`、`CLUSTER_UID`（Pod 里由 downward API 提供）。

**需要观察的现象 / 预期结果**：能够画出 `POD_UID/CLUSTER_UID 环境变量 → initializeConfig → DeploymentContext → json.Marshal → deployment_ctx.json` 这条数据流。

> 待本地验证：若你有 Plus 环境，可在 init 容器日志看到 `"Finished initializing configuration"`，并在数据面 Pod 的 main-includes 目录找到 `deployment_ctx.json`，内容形如 `{"cluster_id":"...","installation_id":"...","integration":"ngf"}`（指针字段非空时才出现）。OSS 环境则不会生成该文件。

#### 4.3.5 小练习与答案

**练习 1**：`DeploymentContext` 里为什么 `Integration` 是值类型 `string`，而 `InstallationID` / `ClusterID` 是指针 `*string`？

**参考答案**：`Integration` 固定为 `"ngf"`，永远非空，用值类型即可。`InstallationID` / `ClusterID` 带 `json:"...,omitempty"` 标签，用指针是为了能区分「字段缺失（nil，序列化时省略）」与「空字符串」，避免在 licensing 上下文里写入无意义的空值。

**练习 2**：`deployment_ctx.json` 的文件权限是 `0644` 还是 `0640`？由什么决定？

**参考答案**：是 `0644`。[generator.go:173](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/generator.go#L173) 把 `Permissions` 设为 `file.RegularFileMode`（`"0644"`），`file.Convert` 据此映射成 `TypeRegular`，`file.Write` 对 `TypeRegular` 用 `0o644` 落盘。只有密钥类文件才用 `0640`。

---

## 5. 综合实践

把本讲的三个模块串起来，完成一个「**安装前依赖梳理**」任务：

**场景**：你要向同事解释「为什么 NGF 在 Helm install 时必须先跑一个 cert-generator Job，以及数据面 Pod 的 init 容器在做什么」。

**要求**：

1. 用一张时序图说明 Helm `pre-install` 阶段发生的事：
   - `cert-generator` Job 启动 → 运行 `generate-certs` → 从 `POD_NAMESPACE` 取命名空间 → 生成 CA/server/client → 创建 `server-tls`、`agent-tls` 两个 Secret。
   - 提示：参考 [certs-job.yaml:116](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/charts/nginx-gateway-fabric/templates/certs-job.yaml#L116)（`helm.sh/hook: pre-install, pre-upgrade`）与 [commands.go:757-786](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L757-L786)（RunE 流程）。
2. 解释「为什么 Job 必须在 Deployment 之前完成」：控制面 gRPC 服务端启动时要从 `server-tls` 读取证书（见 [grpc.go:30-32](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/grpc/grpc.go#L30-L32)），Secret 不存在会启动失败。
3. 用一张图说明数据面 Pod 的 init 容器（`initialize`）→ 主容器（NGINX + Agent）的顺序，并标注：拷贝了哪些初始文件、Plus 下额外写了什么文件、`agent-tls` 是被谁（Agent 客户端）消费的。

**预期产出**：两张图 + 一段文字结论。结论应点明：**`generate-certs` 生产信任材料，`initialize` 摆好运行时静态文件**；二者都是一次性命令，一个跑在 Helm Job、一个跑在 init 容器，共同保证主进程启动时「证书就位、配置就位」。

## 6. 本讲小结

- `initialize` 是数据面 init 容器里的一次性命令：先按 `--source`/`--destination` 一一对应拷贝初始文件，再（仅 Plus）生成 `deployment_ctx.json`；非 Plus 在拷贝后直接返回。
- `generate-certs` 自签一套 **3 年有效期、RSA 2048 位** 的证书：一份自签 CA，再用 CA 签服务端、客户端证书，分别装进 `server-tls` 与 `agent-tls` 两个 Secret。
- 两个 Secret 共享同一份 `ca.crt`，构成 mTLS 的共同信任根；`server-tls` 归控制面 gRPC 服务端，`agent-tls` 归数据面 Agent gRPC 客户端。
- 控制面 gRPC 服务端用 `RequireAndVerifyClientCert` **强制校验客户端证书**，把「单向 TLS」升级为「双向 mTLS」。
- `createSecrets` 是幂等的：未带 `--overwrite` 时已存在的 Secret 会被跳过；带 `--overwrite` 且 `Data` 真变化时才 Update。
- Plus 的 `deployment_ctx.json` 由 `GenerateDeploymentContext` 把 `DeploymentContext` JSON 序列化而成，字段来自 Pod 的 downward API 环境变量（`POD_UID`/`CLUSTER_UID`）。
- 文件落盘统一走 `internal/framework/file` 的 `OSFileManager` 接口与 `Write`，用接口便于测试注入 fake，权限按文件类型区分（普通 `0644` / 密钥 `0640`）。

## 7. 下一步学习建议

本讲聚焦「安装期一次性命令」。接下来建议：

- 沿证书链继续看**消费侧**：进入 [internal/controller/nginx/agent/grpc/grpc.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/grpc/grpc.go)，理解 gRPC 服务端如何 `buildTLSCredentials` 并支持证书热轮换（`GetConfigForClient`）。这一主题属于 **u7（数据面通信）** 单元。
- 回到控制面主流程：本讲的证书 Secret 与 `--agent-tls-secret` flag 最终被 `controller` 命令消费，进入 **u3（Manager 组装）**。建议接着学 [u3-l1](u3-l1-manager-startup-overview.md)，看 `StartManager` 如何把这些 flag 汇成运行配置。
- 想了解 Plus licensing 全链路：可对照 [internal/controller/licensing](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/licensing) 目录（u12-l2 会讲到），把本讲的 `deployment_ctx.json` 与运行时用量上报串起来。
