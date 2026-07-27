# Authenticode-подпись Windows

SmartScreen оценивает цифровую подпись и репутацию издателя. Неподписанная закрытая бета может показывать предупреждение даже при корректном SHA-256.

Для production нужен доверенный Authenticode code-signing certificate (обычный OV или аппаратно/облачно защищённый EV). Сертификат, private key и пароль не хранятся в Git. Pipeline получает thumbprint через защищённую CI/environment переменную `CREATOR_ASSISTANT_SIGN_CERT_SHA1`, вызывает `signtool sign` с SHA-256 и trusted timestamp, затем выполняет `signtool verify /pa`.

SHA-256 релизного файла вычисляется только после окончательной подписи. Если сертификата нет, build завершается с явной маркировкой `UNSIGNED BETA`; самоподписанный сертификат не выдаётся за доверенную production-подпись.
