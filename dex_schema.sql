/* ------------------------------------------------------------
   Lightweight Dex tables for ML feature enrichment
   Sources:
     - https://play.pokemonshowdown.com/data/pokedex.json
     - https://play.pokemonshowdown.com/data/moves.json
   Type effectiveness:
     - Standard modern (Gen 6+) chart multipliers.
   ------------------------------------------------------------ */

IF OBJECT_ID('dbo.DexPokemon','U') IS NULL
BEGIN
  CREATE TABLE dbo.DexPokemon (
    PokemonId        VARCHAR(64)  NOT NULL PRIMARY KEY, -- Showdown id (toID), e.g. 'landorustherian'
    DisplayName      NVARCHAR(96) NOT NULL,              -- e.g. 'Landorus-Therian'
    Type1            VARCHAR(16)  NOT NULL,
    Type2            VARCHAR(16)  NULL,
    BaseHP           SMALLINT     NULL,
    BaseAtk          SMALLINT     NULL,
    BaseDef          SMALLINT     NULL,
    BaseSpA          SMALLINT     NULL,
    BaseSpD          SMALLINT     NULL,
    BaseSpe          SMALLINT     NULL,
    BST              SMALLINT     NULL,
    WeightKg         DECIMAL(8,2) NULL,
    CreatedAt        DATETIME2    NOT NULL CONSTRAINT DF_DexPokemon_CreatedAt DEFAULT (SYSUTCDATETIME())
  );

  CREATE INDEX IX_DexPokemon_DisplayName ON dbo.DexPokemon(DisplayName);
END
GO

IF OBJECT_ID('dbo.DexMove','U') IS NULL
BEGIN
  CREATE TABLE dbo.DexMove (
    MoveId           VARCHAR(64)  NOT NULL PRIMARY KEY,  -- Showdown id (toID), e.g. 'earthquake'
    MoveName         NVARCHAR(96) NOT NULL,              -- e.g. 'Earthquake'
    TypeName         VARCHAR(16)  NULL,
    Category         VARCHAR(16)  NULL,                  -- Physical/Special/Status
    BasePower        SMALLINT     NULL,
    Accuracy         SMALLINT     NULL,                  -- percent, null if true/always hits
    Priority         SMALLINT     NULL,
    Target           VARCHAR(32)  NULL,
    FlagsJson        NVARCHAR(MAX) NULL,                 -- raw flags dict (optional)
    CreatedAt        DATETIME2    NOT NULL CONSTRAINT DF_DexMove_CreatedAt DEFAULT (SYSUTCDATETIME())
  );

  CREATE INDEX IX_DexMove_MoveName ON dbo.DexMove(MoveName);
  CREATE INDEX IX_DexMove_TypeCat ON dbo.DexMove(TypeName, Category);
END
GO

IF OBJECT_ID('dbo.DexTypeEffectiveness','U') IS NULL
BEGIN
  CREATE TABLE dbo.DexTypeEffectiveness (
    AttackingType    VARCHAR(16) NOT NULL,
    DefendingType    VARCHAR(16) NOT NULL,
    Multiplier       DECIMAL(3,2) NOT NULL, -- 0, 0.5, 1, 2
    CONSTRAINT PK_DexTypeEffectiveness PRIMARY KEY (AttackingType, DefendingType)
  );
END
GO
