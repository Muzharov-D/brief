// Огонёк.app: запуск brief из приложения. У расписания и разрешений macOS тогда одно понятное имя и значок.
// Без аргументов (двойной щелчок) открывает настройку, с аргументами передаёт их brief.
import Foundation
let home = ProcessInfo.processInfo.environment["BRIEF_HOME"] ?? (NSHomeDirectory() + "/.brief")
let brief = home + "/bin/brief"
var args = Array(CommandLine.arguments.dropFirst())
if args.isEmpty || args[0].hasPrefix("-psn") { args = ["setup"] }
let argv: [UnsafeMutablePointer<CChar>?] = ([brief] + args).map { strdup($0) } + [nil]
execv(brief, argv)
perror("brief")
exit(1)
