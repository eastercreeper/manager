package manager

import (
 "context"
 "errors"
 "fmt"
 "net/http"
 "net/url"
 "os"
 "path/filepath"
 "strings"
 "time"

 "seanime/internal/core"
 "seanime/internal/database/models"
 "seanime/internal/jellyfin"
 "seanime/internal/mediastream/videofile"
 qbittorrent "seanime/internal/torrent_clients/qbittorrent"
 qbmodel "seanime/internal/torrent_clients/qbittorrent/model"

 "github.com/labstack/echo/v4"
 "github.com/rs/zerolog"
 "gorm.io/gorm"
)

type Service struct { app *core.App; db *gorm.DB; jf *jellyfin.Client }

func Register(app *core.App, v1 *echo.Group) {
 if app == nil || app.Database == nil { return }
 s := &Service{app:app,db:app.Database.Gorm()}
 s.installScannerMappingHook()
 if err := s.db.AutoMigrate(&JellyfinSeries{},&JellyfinEpisode{},&ReleaseApproval{},&MediaVerification{}); err != nil {
  app.Logger.Error().Err(err).Msg("manager: extension schema migration failed"); return
 }
 if base,key:=os.Getenv("JELLYFIN_URL"),os.Getenv("JELLYFIN_API_KEY"); base!=""&&key!="" {
  client,err:=jellyfin.NewClient(base,key,&http.Client{Timeout:25*time.Second})
  if err!=nil { app.Logger.Error().Err(err).Msg("manager: invalid Jellyfin configuration") } else { s.jf=client }
 }
 g:=v1.Group("/manager")
 if token:=strings.TrimSpace(os.Getenv("MANAGER_API_TOKEN")); token!="" { g.Use(func(next echo.HandlerFunc) echo.HandlerFunc { return func(c echo.Context) error { // The dashboard must be reachable so the user can enter the token.
 // All manager API endpoints remain protected by the Bearer token.
 if c.Request().URL.Path!="/api/v1/manager/ui" && c.Request().Header.Get("Authorization")!="Bearer "+token { return c.JSON(401,map[string]string{"error":"unauthorized"}) }; return next(c) } }) }
 g.GET("/ui",s.ui)
 g.GET("/health",s.health)
 g.GET("/jellyfin/status",s.jellyfinStatus)
 g.POST("/jellyfin/sync",s.syncJellyfin)
 g.GET("/library",s.library)
 g.PUT("/mappings/:jellyfinId",s.setMapping)
 g.GET("/approvals",s.listApprovals)
 g.POST("/approvals",s.createApproval)
 g.POST("/approvals/:id/approve",s.approve)
 g.POST("/approvals/:id/reject",s.reject)
 g.POST("/episodes/:jellyfinId/verify",s.verifyEpisode)
 g.POST("/downloads/reconcile",s.reconcileDownloads)
}
func (s *Service) jellyfin() (*jellyfin.Client,error) {
 if s.jf==nil { return nil,errors.New("configure JELLYFIN_URL and JELLYFIN_API_KEY") }; return s.jf,nil
}
func (s *Service) health(c echo.Context) error {
 return c.JSON(200,map[string]any{"ok":true,"application":"anime-manager","foundation":"Seanime","database":"Seanime GORM/SQLite"})
}
func (s *Service) jellyfinStatus(c echo.Context) error {
 jf,err:=s.jellyfin(); if err!=nil{return c.JSON(503,map[string]string{"error":err.Error()})}
 ctx,cancel:=context.WithTimeout(c.Request().Context(),20*time.Second);defer cancel()
 info,err:=jf.GetSystemInfo(ctx);if err!=nil{return c.JSON(502,map[string]string{"error":err.Error()})}
 return c.JSON(200,info)
}
func (s *Service) syncJellyfin(c echo.Context) error {
 jf,err:=s.jellyfin();if err!=nil{return c.JSON(503,map[string]string{"error":err.Error()})}
 ctx,cancel:=context.WithTimeout(c.Request().Context(),3*time.Minute);defer cancel()
 series,err:=jf.ListSeries(ctx,200);if err!=nil{return c.JSON(502,map[string]string{"error":err.Error()})}
 now:=time.Now().UTC(); seriesCount,episodeCount:=0,0
 for _,item:=range series {
  if item.ID==""{continue}
  rec:=JellyfinSeries{JellyfinID:item.ID}
  if err:=s.db.Where("jellyfin_id = ?",item.ID).Assign(map[string]any{"name":item.Name,"path":item.Path,"last_seen_at":now}).FirstOrCreate(&rec).Error;err!=nil{return c.JSON(500,map[string]string{"error":"save series: "+err.Error()})}
  seriesCount++
  episodes,err:=jf.ListEpisodes(ctx,item.ID);if err!=nil{return c.JSON(502,map[string]string{"error":fmt.Sprintf("episodes for %s: %v",item.Name,err)})}
  for _,ep:=range episodes {
   if ep.ID==""{continue}
   season,number:=0,0;if ep.ParentIndexNumber!=nil{season=*ep.ParentIndexNumber};if ep.IndexNumber!=nil{number=*ep.IndexNumber}
   row:=JellyfinEpisode{JellyfinID:ep.ID}
   values:=map[string]any{"series_id":item.ID,"series_name":item.Name,"name":ep.Name,"path":ep.Path,"season_number":season,"episode_number":number,"last_seen_at":now}
   if err:=s.db.Where("jellyfin_id = ?",ep.ID).Assign(values).FirstOrCreate(&row).Error;err!=nil{return c.JSON(500,map[string]string{"error":"save episode: "+err.Error()})}
   episodeCount++
  }
 }
 // Point Seanime's own scanner at the folders Jellyfin reports. The scanner,
 // AniList platform, metadata provider, and local-file cache remain upstream
 // implementations; this extension only supplies Jellyfin's authoritative paths.
 scanQueued:=false
 if s.app.Settings!=nil && s.app.Settings.Library!=nil && s.app.AutoScanner!=nil {
  paths:=make([]string,0,len(series));seen:=map[string]bool{}
  for _,item:=range series { p:=filepath.Clean(strings.TrimSpace(item.Path));if filepath.IsAbs(p)&&!seen[p] { if st,e:=os.Stat(p);e==nil&&st.IsDir(){seen[p]=true;paths=append(paths,p)} } }
  if len(paths)>0 {
   s.app.Settings.Library.LibraryPath=paths[0]
   s.app.Settings.Library.LibraryPaths=models.LibraryPaths(paths[1:])
   if err:=s.db.Save(s.app.Settings).Error;err==nil {
    s.app.AutoScanner.SetSettings(*s.app.Settings.Library)
    go s.app.AutoScanner.RunNow()
    scanQueued=true
   } else { s.app.Logger.Error().Err(err).Msg("manager: could not persist Jellyfin scanner paths") }
  }
 }
 return c.JSON(200,map[string]any{"series":seriesCount,"episodes":episodeCount,"syncedAt":now,"seanimeScanQueued":scanQueued})
}
func (s *Service) library(c echo.Context) error {
 var shows []JellyfinSeries;var eps []JellyfinEpisode
 if err:=s.db.Order("name asc").Find(&shows).Error;err!=nil{return c.JSON(500,map[string]string{"error":err.Error()})}
 if err:=s.db.Order("series_name,season_number,episode_number").Find(&eps).Error;err!=nil{return c.JSON(500,map[string]string{"error":err.Error()})}
 return c.JSON(200,map[string]any{"series":shows,"episodes":eps})
}
func (s *Service) setMapping(c echo.Context) error {
 id:=strings.TrimSpace(c.Param("jellyfinId"));var body struct{AniListID int `json:"anilistId"`}
 if id==""||c.Bind(&body)!=nil||body.AniListID<0{return c.JSON(400,map[string]string{"error":"jellyfinId and a non-negative anilistId are required; use 0 to clear a mapping"})}
 r:=s.db.Model(&JellyfinSeries{}).Where("jellyfin_id = ?",id).Update("ani_list_id",body.AniListID)
 if r.Error!=nil{return c.JSON(500,map[string]string{"error":r.Error.Error()})};if r.RowsAffected==0{return c.JSON(404,map[string]string{"error":"series not found; sync Jellyfin first"})}
 queued:=s.app.AutoScanner!=nil
 if queued{go s.app.AutoScanner.RunNow()}
 return c.JSON(200,map[string]any{"jellyfinId":id,"anilistId":body.AniListID,"scanQueued":queued})
}
func (s *Service) listApprovals(c echo.Context) error {
 var rows []ReleaseApproval;if err:=s.db.Order("created_at desc").Limit(500).Find(&rows).Error;err!=nil{return c.JSON(500,map[string]string{"error":err.Error()})};return c.JSON(200,rows)
}
func (s *Service) createApproval(c echo.Context) error {
 var b struct{Title string `json:"title"`;Link string `json:"link"`;MediaID int `json:"mediaId"`;EpisodeNumber int `json:"episodeNumber"`;SeasonNumber *int `json:"seasonNumber"`;JellyfinSeriesID string `json:"jellyfinSeriesId"`;SavePath string `json:"savePath"`;Category string `json:"category"`}
 if c.Bind(&b)!=nil{return c.JSON(400,map[string]string{"error":"invalid JSON body"})}
 u,err:=url.Parse(strings.TrimSpace(b.Link));if err!=nil||(u.Scheme!="magnet"&&u.Scheme!="http"&&u.Scheme!="https"){return c.JSON(400,map[string]string{"error":"link must be magnet, HTTP, or HTTPS"})}
 if strings.TrimSpace(b.Title)==""||!filepath.IsAbs(b.SavePath){return c.JSON(400,map[string]string{"error":"title and absolute savePath are required"})}
 season:=1;if b.SeasonNumber!=nil{season=*b.SeasonNumber};if season<0||season>99{return c.JSON(400,map[string]string{"error":"seasonNumber must be between 0 and 99"})};row:=ReleaseApproval{Title:b.Title,Link:b.Link,MediaID:b.MediaID,EpisodeNumber:b.EpisodeNumber,SeasonNumber:season,JellyfinSeriesID:strings.TrimSpace(b.JellyfinSeriesID),SavePath:filepath.Clean(b.SavePath),Category:b.Category,Status:"pending"}
 if err:=s.db.Create(&row).Error;err!=nil{return c.JSON(500,map[string]string{"error":err.Error()})};return c.JSON(201,row)
}
func (s *Service) qbClient() (*qbittorrent.Client,error) {
 raw:=strings.TrimSpace(os.Getenv("QBITTORRENT_URL"));if raw==""{return nil,errors.New("configure QBITTORRENT_URL, QBITTORRENT_USERNAME, QBITTORRENT_PASSWORD")}
 u,err:=url.Parse(raw);if err!=nil||u.Hostname()==""{return nil,errors.New("invalid QBITTORRENT_URL")}
 port:=0;if u.Port()!=""{fmt.Sscan(u.Port(),&port)}
 host:="http://"+u.Hostname();if u.Scheme=="https"{host="https://"+u.Hostname()}
 logger:=zerolog.Nop()
 client:=qbittorrent.NewClient(&qbittorrent.NewClientOptions{Logger:&logger,Host:host,Port:port,Username:os.Getenv("QBITTORRENT_USERNAME"),Password:os.Getenv("QBITTORRENT_PASSWORD")})
 if err:=client.Login();err!=nil{return nil,fmt.Errorf("qBittorrent login: %w",err)};return client,nil
}
func (s *Service) approve(c echo.Context) error {
 var row ReleaseApproval;if err:=s.db.First(&row,c.Param("id")).Error;err!=nil{return c.JSON(404,map[string]string{"error":"approval not found"})}
 if row.Status!="pending"{return c.JSON(409,map[string]string{"error":"approval is not pending"})}
 client,err:=s.qbClient();if err!=nil{return c.JSON(503,map[string]string{"error":err.Error()})}
 opts:=&qbmodel.AddTorrentsOptions{Savepath:row.SavePath,Category:row.Category,Paused:"false",Tags:"anime-manager"}
 if err:=client.Torrent.AddURLs([]string{row.Link},opts);err!=nil{return c.JSON(502,map[string]string{"error":"qBittorrent rejected torrent: "+err.Error()})}
 now:=time.Now().UTC();if err:=s.db.Model(&row).Updates(map[string]any{"status":"approved","decision_at":now}).Error;err!=nil{return c.JSON(500,map[string]string{"error":err.Error()})}
 row.Status="approved";row.DecisionAt=&now;return c.JSON(200,row)
}
func (s *Service) reject(c echo.Context) error {
 var row ReleaseApproval;if err:=s.db.First(&row,c.Param("id")).Error;err!=nil{return c.JSON(404,map[string]string{"error":"approval not found"})}
 if row.Status!="pending"{return c.JSON(409,map[string]string{"error":"approval is not pending"})}
 now:=time.Now().UTC();if err:=s.db.Model(&row).Updates(map[string]any{"status":"rejected","decision_at":now}).Error;err!=nil{return c.JSON(500,map[string]string{"error":err.Error()})}
 row.Status="rejected";row.DecisionAt=&now;return c.JSON(200,row)
}
func (s *Service) verifyEpisode(c echo.Context) error {
 var ep JellyfinEpisode;if err:=s.db.Where("jellyfin_id = ?",c.Param("jellyfinId")).First(&ep).Error;err!=nil{return c.JSON(404,map[string]string{"error":"episode not found; sync Jellyfin first"})}
 if ep.Path==""{return c.JSON(422,map[string]string{"error":"Jellyfin did not provide a filesystem path"})}
 result:=MediaVerification{JellyfinEpisodeID:ep.JellyfinID,Path:ep.Path,CheckedAt:time.Now().UTC()}
 info,err:=videofile.FfprobeGetInfo("",ep.Path,"")
 if err!=nil{result.Error=err.Error()}else{
  result.Readable=true
  for _,sub:=range info.Subtitles{
   lang,codec,title:="","", "";if sub.Language!=nil{lang=strings.ToLower(*sub.Language)};codec=strings.ToLower(sub.Codec);if sub.Title!=nil{title=strings.ToLower(*sub.Title)}
   result.SubtitleTracks+=fmt.Sprintf("%s|%s|%s|external=%t\n",lang,codec,title,sub.IsExternal)
   english:=lang=="eng"||lang=="en"||strings.Contains(title,"english")
   ass:=codec=="ass"||codec=="ssa"||codec=="substation alpha"||codec=="s_text/ass"
   if english&&ass{result.HasEnglishASS=true}
  }
  stem:=strings.TrimSuffix(ep.Path,filepath.Ext(ep.Path))
  files,_:=filepath.Glob(filepath.Dir(ep.Path)+string(os.PathSeparator)+filepath.Base(stem)+".*")
  for _,f:=range files{n:=strings.ToLower(filepath.Base(f));ext:=strings.ToLower(filepath.Ext(f));if (ext==".ass"||ext==".ssa")&&(strings.Contains(n,".en.")||strings.Contains(n,".eng.")||strings.Contains(n,"english")){result.HasEnglishASS=true}}
 }
 if err:=s.db.Where("jellyfin_episode_id = ?",ep.JellyfinID).Assign(result).FirstOrCreate(&result).Error;err!=nil{return c.JSON(500,map[string]string{"error":err.Error()})}
 return c.JSON(200,result)
}

 
// reconcileDownloads checks approved torrents and imports completed media into the
// selected Jellyfin series/season directory. Source files remain in qBittorrent's
// save path so active seeding is preserved.
func (s *Service) reconcileDownloads(c echo.Context) error {
 client,err:=s.qbClient();if err!=nil{return c.JSON(503,map[string]string{"error":err.Error()})}
 list,err:=client.Torrent.GetList(&qbmodel.GetTorrentListOptions{Filter:qbmodel.FilterAll})
 if err!=nil{return c.JSON(502,map[string]string{"error":"qBittorrent list: "+err.Error()})}
 var approvals []ReleaseApproval
 if err:=s.db.Where("status IN ?",[]string{"approved","downloaded"}).Find(&approvals).Error;err!=nil{return c.JSON(500,map[string]string{"error":err.Error()})}
 newlyDownloaded,imported:=0,0
 importErrors:=make([]map[string]any,0)
 for i:=range approvals {
  a:=&approvals[i]
  for _,t:=range list {
   if !strings.Contains(t.Tags,"anime-manager"){continue}
   match:=a.QBittorrentHash!=""&&strings.EqualFold(a.QBittorrentHash,t.Hash)
   if !match { if h:=magnetHash(a.Link);h!=""{match=strings.EqualFold(h,t.InfohashV1)||strings.EqualFold(h,t.InfohashV2)} }
   if !match&&strings.TrimSpace(a.Title)!=""{match=strings.Contains(strings.ToLower(t.Name),strings.ToLower(a.Title))}
   if !match||t.Progress<0.9999{continue}
   if a.Status=="approved" {
    now:=time.Now().UTC()
    if err:=s.db.Model(a).Updates(map[string]any{"status":"downloaded","qbittorrent_hash":t.Hash,"decision_at":now,"import_error":""}).Error;err!=nil {
     importErrors=append(importErrors,map[string]any{"approvalId":a.ID,"error":"save completion state: "+err.Error()});break
    }
    a.Status="downloaded";a.QBittorrentHash=t.Hash;a.DecisionAt=&now;newlyDownloaded++
   }
   paths,importErr:=s.importCompletedTorrent(a,t,client)
   if importErr!=nil {
    _=s.db.Model(a).Update("import_error",importErr.Error()).Error
    a.ImportError=importErr.Error()
    importErrors=append(importErrors,map[string]any{"approvalId":a.ID,"title":a.Title,"error":importErr.Error()})
    break
   }
   markImported(a,paths)
   if err:=s.db.Model(a).Updates(map[string]any{"status":a.Status,"imported_at":a.ImportedAt,"imported_paths":a.ImportedPaths,"import_error":""}).Error;err!=nil {
    importErrors=append(importErrors,map[string]any{"approvalId":a.ID,"error":"save import state: "+err.Error()})
   } else { imported++ }
   break
  }
 }
 jf,jfErr:=s.jellyfin()
 refreshed:=false
 if (newlyDownloaded>0||imported>0)&&jfErr==nil {
  ctx,cancel:=context.WithTimeout(c.Request().Context(),30*time.Second);defer cancel()
  if err:=jf.RefreshLibrary(ctx);err==nil{refreshed=true}
 }
 return c.JSON(200,map[string]any{
  "checked":len(approvals),"newlyDownloaded":newlyDownloaded,"imported":imported,
  "jellyfinRefreshRequested":refreshed,"importErrors":importErrors,
  "note":"Imported files are copied into Jellyfin's season folders; qBittorrent source files are retained for seeding. Sync Jellyfin and verify the resulting episode after the refresh.",
 })
}

func magnetHash(link string) string {
 u,err:=url.Parse(link);if err!=nil{return ""}
 for _,xt:=range u.Query()["xt"] {
  lower:=strings.ToLower(xt)
  if strings.HasPrefix(lower,"urn:btih:"){return xt[len("urn:btih:"):]}
 }
 return ""
}
