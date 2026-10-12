# WorkManager (pulled in by home_widget) instantiates its Room database
# (androidx.work.impl.WorkDatabase_Impl) by reflection. R8 strips that
# generated class / its constructor in release builds, and the app then
# crashes on launch in androidx.startup.InitializationProvider with
# "Failed to create an instance of androidx.work.impl.WorkDatabase".
-keep class * extends androidx.room.RoomDatabase { <init>(); }
-keep class androidx.work.impl.WorkDatabase_Impl { *; }
